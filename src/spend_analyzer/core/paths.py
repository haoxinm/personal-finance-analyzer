"""Data directory locations (§3.11).

All application state lives under one data directory: the SQLite database, statement PDF copies,
the extract-only-if-enabled debug cache, layout-spec YAML mirrors, `config.toml`, and log files.

The default location is the platform's per-user application-data directory:

- macOS: ``~/Library/Application Support/SpendAnalyzer``
- Windows: ``%LOCALAPPDATA%\\SpendAnalyzer`` (falling back to ``%APPDATA%``, then
  ``~/AppData/Local``)
- Linux / other POSIX: ``$XDG_DATA_HOME/spend-analyzer`` (default ``~/.local/share/spend-analyzer``)

Set `SPEND_ANALYZER_HOME` to override it — every test in this repository does, so CI never
touches a real user's data directory.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
from pathlib import Path

#: Environment variable that overrides the default data directory. Read fresh on every call so
#: tests can change it mid-process via `monkeypatch.setenv`.
SPEND_ANALYZER_HOME_ENV = "SPEND_ANALYZER_HOME"


def default_home(platform: str | None = None) -> Path:
    """The per-user data directory for ``platform`` (default: the running one, `sys.platform`)."""
    plat = platform if platform is not None else sys.platform
    if plat == "darwin":
        return Path.home() / "Library" / "Application Support" / "SpendAnalyzer"
    if plat.startswith("win") or plat == "cygwin":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "SpendAnalyzer"
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return root / "spend-analyzer"


#: Directories created (mode 0700) inside the data home.
_SUBDIRS = ("statements", "extract_cache", "specs", "logs")


def get_home() -> Path:
    """Return the data directory, honoring `SPEND_ANALYZER_HOME`. Does not create it."""
    override = os.environ.get(SPEND_ANALYZER_HOME_ENV)
    return Path(override).expanduser() if override else default_home()


def ensure_home() -> Path:
    """Return the data directory, creating it and its standard subdirectories (mode 0700) if
    absent. Safe to call repeatedly."""
    home = get_home()
    create_home_layout(home)
    return home


def create_home_layout(home: Path) -> None:
    """Create ``home`` and its standard subdirectories (mode 0700) if absent. Takes an explicit
    path so a restore can lay out a staging directory that will later be renamed to the real one."""
    _mkdir_0700(home)
    for name in _SUBDIRS:
        _mkdir_0700(home / name)


def _mkdir_0700(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    # Some filesystems (e.g. certain CI/container mounts) don't support chmod, and on Windows
    # chmod only toggles the read-only bit (the per-user profile directory's ACLs already keep
    # other users out); the directory still exists and works either way.
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRWXU)  # 0o700: owner rwx only


def db_path() -> Path:
    return get_home() / "spend.db"


def config_path() -> Path:
    return get_home() / "config.toml"


def statements_dir() -> Path:
    return get_home() / "statements"


def tax_staging_dir() -> Path:
    """Short-lived, never-backed-up copies of tax PDFs re-selected by the user (not created by
    `ensure_home`; see `ingest/tax/staging.py`)."""
    return get_home() / "tax_staging"


def extract_cache_dir() -> Path:
    return get_home() / "extract_cache"


def specs_dir() -> Path:
    return get_home() / "specs"


def logs_dir() -> Path:
    return get_home() / "logs"
