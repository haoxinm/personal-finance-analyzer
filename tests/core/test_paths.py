from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from personal_finance_analyzer.core import paths


def test_get_home_honors_env_override(home: Path) -> None:
    assert paths.get_home() == home


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_ensure_home_creates_subdirs_with_0700(home: Path) -> None:
    result = paths.ensure_home()
    assert result == home
    for name in ("statements", "extract_cache", "specs", "logs"):
        sub = home / name
        assert sub.is_dir()
        mode = stat.S_IMODE(sub.stat().st_mode)
        assert mode == stat.S_IRWXU


def test_derived_paths_live_under_home(home: Path) -> None:
    assert paths.db_path() == home / "finance.db"
    assert paths.config_path() == home / "config.toml"
    assert paths.statements_dir() == home / "statements"
    assert paths.extract_cache_dir() == home / "extract_cache"
    assert paths.specs_dir() == home / "specs"
    assert paths.logs_dir() == home / "logs"


def test_default_home_is_platform_specific(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    assert paths.default_home("darwin") == (
        Path.home() / "Library" / "Application Support" / "PersonalFinanceAnalyzer"
    )
    assert (
        paths.default_home("linux")
        == Path.home() / ".local" / "share" / "personal-finance-analyzer"
    )
    assert (
        paths.default_home("win32") == Path.home() / "AppData" / "Local" / "PersonalFinanceAnalyzer"
    )


def test_default_home_honors_platform_env_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert paths.default_home("linux") == tmp_path / "xdg" / "personal-finance-analyzer"
    assert paths.default_home("win32") == tmp_path / "local" / "PersonalFinanceAnalyzer"
