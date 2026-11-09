"""Data directory locations (§3.11).

All application state lives under one data directory: the SQLite database, statement PDF copies,
the extract-only-if-enabled debug cache, layout-spec YAML mirrors, `config.toml`, and log files.

The default location is the platform's per-user application-data directory:

- macOS: ``~/Library/Application Support/PersonalFinanceAnalyzer``
- Windows: ``%LOCALAPPDATA%\\PersonalFinanceAnalyzer`` (falling back to ``%APPDATA%``, then
  ``~/AppData/Local``)
- Linux / other POSIX: ``$XDG_DATA_HOME/personal-finance-analyzer`` (default
  ``~/.local/share/personal-finance-analyzer``)

Set `PFA_HOME` to override it — every test in this repository does, so CI never touches a real
user's data directory. The product's former name read `SPEND_ANALYZER_HOME`, which is still
honored when `PFA_HOME` is unset.

**Upgrading from "Spend Analyzer".** The product used to keep its data in ``SpendAnalyzer`` /
``spend-analyzer`` (see `legacy_default_home`) and its database in ``spend.db``.

Resolving a path never changes the disk: `get_home`, `db_path` and every `*_dir()` helper are pure
(they only ``stat``), so a read-only command (``pfa llm status``, a backup listing, ``pfa doctor``)
can never rename a user's data. While the old directory has not been carried over they simply
keep pointing at it. The carry-over happens only in `prepare_home`, which the commands that open
or initialize the database call once at startup (``pfa serve``, ``pfa migrate``, every command
that opens the engine, the API app's own startup), never for a directory named by an environment
override:

- the new default is absent and the old one exists: the old directory is *renamed* to the new
  one (atomic on the same filesystem). If the rename fails the old directory keeps being used and
  a warning says why; nothing is ever copied and then deleted;
- the old directory is not moved while another process has its database open (an old
  ``spend-analyzer serve`` still running would keep writing into the old path): it keeps being
  used, with a warning to stop the old server and restart;
- both exist: the new one is used and a warning names the old one (it is left untouched);
- a concurrent start that loses the rename race simply finds the new directory in place.

The database and the kept PDF copies record their absolute path (``statements.stored_path``,
``tax_documents.stored_path``). After a successful move `prepare_home` rewrites the old directory
prefix to the new one in both tables, in one transaction, and in any case `kept_pdf` resolves a
copy relative to the current data directory at read time (``statements/<sha256>.pdf``) when the
recorded path no longer exists, so a path left stale (a crash between the move and the rewrite, a
restored backup from another machine) never loses a PDF.

`prepare_home` then does the same for the file: ``spend.db`` becomes ``finance.db`` (in any data
directory, override or not) when the new name is absent and the database is not in use.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import stat
import sys
import threading
from pathlib import Path

_log = logging.getLogger(__name__)

#: Environment variable that overrides the default data directory. Read fresh on every call so
#: tests can change it mid-process via `monkeypatch.setenv`.
PFA_HOME_ENV = "PFA_HOME"

#: The pre-rename name of `PFA_HOME_ENV`; consulted only when `PFA_HOME` is unset or empty.
LEGACY_HOME_ENV = "SPEND_ANALYZER_HOME"

#: The database file inside the data directory, and its pre-rename name.
DB_FILENAME = "finance.db"
LEGACY_DB_FILENAME = "spend.db"


def default_home(platform: str | None = None) -> Path:
    """The per-user data directory for ``platform`` (default: the running one, `sys.platform`)."""
    return _platform_home("PersonalFinanceAnalyzer", "personal-finance-analyzer", platform)


def legacy_default_home(platform: str | None = None) -> Path:
    """Where the app kept its data before it was renamed (see the module docstring)."""
    return _platform_home("SpendAnalyzer", "spend-analyzer", platform)


def _platform_home(camel: str, kebab: str, platform: str | None) -> Path:
    plat = platform if platform is not None else sys.platform
    if plat == "darwin":
        return Path.home() / "Library" / "Application Support" / camel
    if plat.startswith("win") or plat == "cygwin":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / camel
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return root / kebab


#: Directories created (mode 0700) inside the data home.
_SUBDIRS = ("statements", "extract_cache", "specs", "logs")


def env_home_override() -> Path | None:
    """The data directory named by `PFA_HOME` (else the legacy `SPEND_ANALYZER_HOME`), if any."""
    for name in (PFA_HOME_ENV, LEGACY_HOME_ENV):
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    return None


#: Serializes the legacy-directory carry-over between threads; processes race on the atomic
#: rename itself (see `_resolve_default_home`).
_MIGRATION_LOCK = threading.Lock()
_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: object) -> None:
    if key not in _warned:
        _warned.add(key)
        _log.warning(message, *args)


def get_home() -> Path:
    """Return the data directory: `PFA_HOME` (or the legacy `SPEND_ANALYZER_HOME`) when set, else
    the platform default. Pure: never creates, renames or writes anything. While a pre-rename
    default directory has not been carried over (see `prepare_home`) it is returned instead, so
    a read-only caller still sees the user's data."""
    override = env_home_override()
    return override if override is not None else _resolve_default_home()


def _resolve_default_home() -> Path:
    new = default_home()
    legacy = legacy_default_home()
    if new == legacy:  # defensive: never treat a directory as its own predecessor
        return new
    if new.exists():
        if legacy.exists():
            _warn_once(
                f"both:{legacy}",
                "both %s and the former data directory %s exist; using %s and leaving the "
                "old one untouched (move anything you still need out of it, then delete it)",
                new,
                legacy,
                new,
            )
        return new
    return legacy if legacy.exists() else new


def prepare_home() -> Path:
    """The explicit startup step of every command that opens or initializes the database: carry a
    pre-rename data directory and database over to their new names (see the module docstring),
    create the directory layout, and return the data directory.

    This is the only function that renames user data. Call it once, early, from an entry point
    (`serve`, `migrate`, the CLI's engine opener, the API app factory); library code and
    read-only commands use the pure `get_home`/`ensure_home` instead."""
    before = get_home()
    migrate_legacy_home()
    home = ensure_home()
    db_file = migrate_legacy_db(home)
    if home != before:  # the data directory was moved: the recorded PDF paths still name the old
        rewrite_stored_paths(db_file, before, home)
    return home


def migrate_legacy_home() -> Path:
    """Rename the pre-rename default data directory to the new one when only it exists. A no-op
    for an environment-override directory. Returns the directory in use afterwards. Mutates:
    call it only from `prepare_home`."""
    if env_home_override() is not None:
        return get_home()
    new = default_home()
    legacy = legacy_default_home()
    if new == legacy:
        return new
    with _MIGRATION_LOCK:
        if new.exists() or not legacy.exists():
            return get_home()  # nothing to move (or a both-exist warning, via get_home)
        busy = _busy_database(legacy)
        if busy is not None:
            _warn_once(
                f"busy-home:{legacy}",
                "the former data directory %s was not moved to %s because its database %s is in "
                "use; continuing to use it (stop the old `spend-analyzer serve`, then restart "
                "to finish the move)",
                legacy,
                new,
                busy,
            )
            return legacy
        try:
            # Atomic when both are on one filesystem (they are siblings), and refuses a
            # non-empty destination, so a process that races us can never be clobbered.
            legacy.rename(new)
        except FileNotFoundError:
            return new  # another process moved it first
        except OSError as exc:
            if new.exists():  # lost a race to a process that created or moved into `new`
                return new
            _warn_once(
                f"rename:{legacy}",
                "could not move the former data directory %s to %s (%s); continuing to use %s",
                legacy,
                new,
                exc,
                legacy,
            )
            return legacy
        _log.info("moved the former data directory %s to %s", legacy, new)
        return new


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
    """The database file in the data directory. Pure: ``finance.db``, or a not-yet-renamed
    ``spend.db`` (see `migrate_legacy_db`) when that is all there is."""
    return resolve_db_file(get_home())


def resolve_db_file(home: Path) -> Path:
    """The database file currently in ``home``, without renaming anything: ``finance.db``, else a
    pre-rename ``spend.db``, else the (absent) ``finance.db`` path. If both exist the new one wins
    and a warning names the old one."""
    new = home / DB_FILENAME
    old = home / LEGACY_DB_FILENAME
    if new.exists():
        if old.exists():
            _warn_once(
                f"both-db:{old}",
                "both %s and the former database %s exist; using %s and leaving the old file "
                "untouched",
                new,
                old,
                new,
            )
        return new
    return old if old.exists() else new


def existing_db_file(home: Path) -> Path:
    """Alias of `resolve_db_file`, for the read-only callers (a backup, a restore's "is it in use"
    check) that must work on a directory `prepare_home` has not visited yet."""
    return resolve_db_file(home)


def migrate_legacy_db(home: Path) -> Path:
    """``home/finance.db``; if only the pre-rename ``home/spend.db`` exists it is renamed to the
    new name first, unless that database is in use (a leftover ``-wal``/``-shm``/``-journal`` that
    survives a checkpoint-and-close means another process has it open, or the move failed): then
    the old file keeps being used, with a warning. If both exist the new one wins and a warning
    names the old one. Safe to call concurrently; the rename is atomic and refuses to overwrite.
    Mutates: call it only from `prepare_home`."""
    new = home / DB_FILENAME
    old = home / LEGACY_DB_FILENAME
    with _MIGRATION_LOCK:
        if new.exists() or not old.exists():
            return resolve_db_file(home)  # nothing to move (or the both-exist warning)
        if _is_busy(old):
            _warn_once(
                f"busy-db:{old}",
                "the former database %s is in use, so it was not renamed to %s; continuing to "
                "use it (stop the old `spend-analyzer serve`, then restart to finish the move)",
                old,
                new.name,
            )
            return old
        try:
            old.rename(new)
        except FileNotFoundError:
            return new  # another process renamed it first
        except OSError as exc:
            if new.exists():
                return new
            _warn_once(
                f"rename-db:{old}",
                "could not rename %s to %s (%s); continuing to use %s",
                old,
                new.name,
                exc,
                old,
            )
            return old
        _log.info("renamed the former database %s to %s", old, new)
        return new


def _busy_database(home: Path) -> Path | None:
    """The database file in ``home`` that another process has open, if any. Checks both names
    (``spend.db`` and ``finance.db``) *before* the directory holding them is renamed."""
    for name in (LEGACY_DB_FILENAME, DB_FILENAME):
        candidate = home / name
        if candidate.exists() and _is_busy(candidate):
            return candidate
    return None


def _is_busy(db_file: Path) -> bool:
    """Whether another process has ``db_file`` open: a SQLite sidecar (``-wal``/``-shm``/
    ``-journal``) that survives a checkpoint-and-close. A leftover log of a crashed run is folded
    back in and does not count."""
    return bool(_sidecars(db_file)) and not _close_cleanly(db_file)


def _sidecars(db_file: Path) -> list[Path]:
    return [
        p for suffix in ("-wal", "-shm", "-journal") if (p := Path(f"{db_file}{suffix}")).exists()
    ]


def _close_cleanly(db_file: Path) -> bool:
    """Fold a leftover write-ahead log of a crashed run back into ``db_file`` (SQLite does that
    when the last connection closes) and report whether no sidecar file remains. A file another
    process still has open keeps its sidecars, so this is also the "is it in use" test."""
    try:
        conn = sqlite3.connect(db_file, timeout=0.5)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return not _sidecars(db_file)


def config_path() -> Path:
    return get_home() / "config.toml"


def statements_dir() -> Path:
    return get_home() / "statements"


def tax_staging_dir() -> Path:
    """Short-lived, never-backed-up copies of tax PDFs re-selected by the user (not created by
    `ensure_home`; see `ingest/tax/staging.py`)."""
    return get_home() / "tax_staging"


def tax_params_dir() -> Path:
    """Downloaded and user-imported federal tax parameter files and the once-a-year check state
    (not created by `ensure_home`; see `core/tax_params.py`)."""
    return get_home() / "tax_params"


def extract_cache_dir() -> Path:
    return get_home() / "extract_cache"


def specs_dir() -> Path:
    return get_home() / "specs"


def logs_dir() -> Path:
    return get_home() / "logs"


def kept_pdf(stored_path: str | None, file_sha256: str) -> Path | None:
    """The kept copy of an imported PDF (a statement, or a tax document), or ``None`` if there is
    none on disk.

    ``stored_path`` is an absolute path recorded when the PDF was imported, so it goes stale when
    the data directory moves (`prepare_home`) or comes from another machine (a restored backup).
    It is used when it still exists; otherwise the copy is looked up where this version of the app
    keeps it, ``statements/<sha256>.pdf`` under the *current* data directory."""
    if stored_path:
        recorded = Path(stored_path)
        if recorded.is_file():
            return recorded
    current = statements_dir() / f"{file_sha256}.pdf"
    return current if current.is_file() else None


#: The tables holding a ``stored_path`` column (names are constants, never caller input).
_STORED_PATH_TABLES = ("statements", "tax_documents")


def rewrite_stored_paths(db_file: Path, old_home: Path, new_home: Path) -> int:
    """Replace the ``old_home`` prefix of every ``stored_path`` in ``statements`` and
    ``tax_documents`` with ``new_home``, in a single transaction, and return how many rows changed.

    Works on the file directly so it needs no migration to have run (a table or column that an old
    database lacks is skipped). Best effort: a failure is logged and leaves the database as it was,
    since `kept_pdf` still finds the copies. Mutates: call it only from `prepare_home`."""
    old_prefixes = {f"{old_home}{os.sep}"}
    with contextlib.suppress(OSError):
        old_prefixes.add(f"{old_home.resolve()}{os.sep}")
    new_prefix = f"{new_home}{os.sep}"
    changed = 0
    if not db_file.is_file():  # nothing to rewrite (and never create an empty database)
        return 0
    try:
        conn = sqlite3.connect(db_file, timeout=5.0, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table in _STORED_PATH_TABLES:
                    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                    if "stored_path" not in columns:
                        continue
                    for old_prefix in sorted(old_prefixes):
                        changed += conn.execute(
                            f"UPDATE {table} SET stored_path = ? || substr(stored_path, ?) "
                            "WHERE substr(stored_path, 1, ?) = ?",
                            (new_prefix, len(old_prefix) + 1, len(old_prefix), old_prefix),
                        ).rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _log.warning(
            "could not update the recorded PDF paths in %s after moving %s to %s (%s); kept "
            "copies are still found under the new directory",
            db_file,
            old_home,
            new_home,
            exc,
        )
        return 0
    if changed:
        _log.info("rewrote %d recorded PDF path(s) from %s to %s", changed, old_home, new_home)
    return changed
