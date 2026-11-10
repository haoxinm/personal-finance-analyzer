from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import stat
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from personal_finance_analyzer.core import paths
from tests.backup.support import seed_home


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
    assert paths.default_home("linux") == (
        Path.home() / ".local" / "share" / "personal-finance-analyzer"
    )
    assert paths.default_home("win32") == (
        Path.home() / "AppData" / "Local" / "PersonalFinanceAnalyzer"
    )
    # The pre-rename locations, which the data directory is carried over from.
    assert paths.legacy_default_home("darwin") == (
        Path.home() / "Library" / "Application Support" / "SpendAnalyzer"
    )
    assert paths.legacy_default_home("linux") == (
        Path.home() / ".local" / "share" / "spend-analyzer"
    )
    assert paths.legacy_default_home("win32") == Path.home() / "AppData" / "Local" / "SpendAnalyzer"


def test_default_home_honors_platform_env_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert paths.default_home("linux") == tmp_path / "xdg" / "personal-finance-analyzer"
    assert paths.default_home("win32") == tmp_path / "local" / "PersonalFinanceAnalyzer"
    assert paths.legacy_default_home("linux") == tmp_path / "xdg" / "spend-analyzer"
    assert paths.legacy_default_home("win32") == tmp_path / "local" / "SpendAnalyzer"


# --- environment overrides -------------------------------------------------------------------


def test_legacy_home_env_is_honored_when_pfa_home_is_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(paths.PFA_HOME_ENV, raising=False)
    monkeypatch.setenv(paths.LEGACY_HOME_ENV, str(tmp_path / "legacy_env"))
    assert paths.get_home() == tmp_path / "legacy_env"


def test_pfa_home_wins_over_the_legacy_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(paths.PFA_HOME_ENV, str(tmp_path / "new_env"))
    monkeypatch.setenv(paths.LEGACY_HOME_ENV, str(tmp_path / "legacy_env"))
    assert paths.get_home() == tmp_path / "new_env"


# --- carrying over the pre-rename data directory ---------------------------------------------


@pytest.fixture
def warnings_logged() -> Iterator[list[str]]:
    """Messages `paths` logs at WARNING, captured on its own logger so the app logger's secret
    redaction (which treats a long absolute path as a secret) never rewrites them."""
    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                messages.append(record.getMessage())

    handler = _Capture()
    logger = logging.getLogger(paths.__name__)
    logger.addHandler(handler)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)


@pytest.fixture
def defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """Default (new, legacy) directories under a scratch parent, with no env override set."""
    monkeypatch.delenv(paths.PFA_HOME_ENV, raising=False)
    monkeypatch.delenv(paths.LEGACY_HOME_ENV, raising=False)
    new = tmp_path / "support" / "PersonalFinanceAnalyzer"
    legacy = tmp_path / "support" / "SpendAnalyzer"
    new.parent.mkdir()
    monkeypatch.setattr(paths, "default_home", lambda platform=None: new)
    monkeypatch.setattr(paths, "legacy_default_home", lambda platform=None: legacy)
    paths._warned.clear()
    return new, legacy


def _tree(root: Path) -> list[tuple[str, int]]:
    """Every path under ``root`` (and its mtime), to prove a call changed nothing."""
    return sorted((str(p), p.stat().st_mtime_ns) for p in [root, *root.rglob("*")])


def test_get_home_is_pure_and_keeps_pointing_at_the_unmigrated_legacy_dir(
    defaults: tuple[Path, Path],
) -> None:
    new, legacy = defaults
    (legacy / "statements").mkdir(parents=True)
    (legacy / "spend.db").write_bytes(b"db")
    before = _tree(legacy.parent)

    assert paths.get_home() == legacy  # reads still see the user's data
    assert paths.config_path() == legacy / "config.toml"
    assert paths.db_path() == legacy / "spend.db"  # not renamed
    assert paths.resolve_db_file(legacy) == legacy / "spend.db"
    assert paths.existing_db_file(legacy) == legacy / "spend.db"

    assert _tree(legacy.parent) == before  # nothing created, renamed or touched
    assert not new.exists()


def test_legacy_default_dir_is_renamed_to_the_new_one(defaults: tuple[Path, Path]) -> None:
    new, legacy = defaults
    (legacy / "statements").mkdir(parents=True)
    (legacy / "statements" / "a.pdf").write_bytes(b"x")
    (legacy / "config.toml").write_text("[llm]\n")

    assert paths.prepare_home() == new
    assert not legacy.exists()
    assert (new / "statements" / "a.pdf").read_bytes() == b"x"
    assert (new / "config.toml").read_text() == "[llm]\n"
    assert paths.get_home() == new
    assert paths.prepare_home() == new  # idempotent


def test_fresh_install_uses_the_new_default_without_creating_anything(
    defaults: tuple[Path, Path],
) -> None:
    new, legacy = defaults
    assert paths.get_home() == new
    assert not new.exists()
    assert not legacy.exists()


def test_prepare_home_on_a_fresh_install_creates_the_new_layout(
    defaults: tuple[Path, Path],
) -> None:
    new, legacy = defaults
    assert paths.prepare_home() == new
    assert (new / "statements").is_dir()
    assert not legacy.exists()


def test_both_default_dirs_use_the_new_one_and_warn(
    defaults: tuple[Path, Path], warnings_logged: list[str]
) -> None:
    new, legacy = defaults
    new.mkdir()
    legacy.mkdir()
    (legacy / "marker").write_text("old")
    assert paths.get_home() == new
    assert paths.prepare_home() == new
    assert (legacy / "marker").read_text() == "old"  # untouched
    assert [m for m in warnings_logged if str(legacy) in m]
    assert len(warnings_logged) == 1  # named once per process, not on every call


def test_failed_rename_falls_back_to_the_old_dir_with_a_warning(
    defaults: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, warnings_logged: list[str]
) -> None:
    new, legacy = defaults
    legacy.mkdir()
    (legacy / "marker").write_text("old")

    def refuse(self: Path, target: object) -> Path:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "rename", refuse)
    assert paths.prepare_home() == legacy or paths.get_home() == legacy
    assert paths.get_home() == legacy
    monkeypatch.undo()
    assert not new.exists()  # nothing was copied
    assert (legacy / "marker").read_text() == "old"
    assert any("could not move" in m for m in warnings_logged)


def test_losing_the_rename_race_finds_the_new_dir(
    defaults: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    new, legacy = defaults
    legacy.mkdir()
    real_rename = Path.rename

    def racing(self: Path, target: Path) -> Path:
        # Another process wins between our check and our rename.
        real_rename(self, target)
        raise FileNotFoundError(self)

    monkeypatch.setattr(Path, "rename", racing)
    assert paths.migrate_legacy_home() == new
    assert new.is_dir()


def test_concurrent_starts_move_the_dir_exactly_once(defaults: tuple[Path, Path]) -> None:
    new, legacy = defaults
    (legacy / "statements").mkdir(parents=True)
    (legacy / "marker").write_text("data")
    results: list[Path] = []

    def start() -> None:
        results.append(paths.prepare_home())

    threads = [threading.Thread(target=start) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [new] * 8
    assert (new / "marker").read_text() == "data"
    assert not legacy.exists()


def test_env_override_never_triggers_the_default_dir_move(
    defaults: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    new, legacy = defaults
    legacy.mkdir()
    monkeypatch.setenv(paths.PFA_HOME_ENV, str(tmp_path / "elsewhere"))
    assert paths.get_home() == tmp_path / "elsewhere"
    assert paths.prepare_home() == tmp_path / "elsewhere"
    assert legacy.is_dir()
    assert not new.exists()


# --- carrying over spend.db ------------------------------------------------------------------


def _make_db(path: Path, *, wal: bool = False) -> None:
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES ('kept')")
    con.commit()
    con.close()


def _value(path: Path) -> str:
    con = sqlite3.connect(path)
    try:
        return str(con.execute("SELECT v FROM t").fetchone()[0])
    finally:
        con.close()


def test_db_path_never_renames_spend_db(home: Path) -> None:
    _make_db(home / "spend.db")
    assert paths.db_path() == home / "spend.db"
    assert paths.db_path() == home / "spend.db"
    assert not (home / "finance.db").exists()
    assert _value(home / "spend.db") == "kept"


def test_spend_db_is_renamed_to_finance_db(home: Path) -> None:
    _make_db(home / "spend.db")
    assert paths.prepare_home() == home
    assert paths.db_path() == home / "finance.db"
    assert not (home / "spend.db").exists()
    assert _value(home / "finance.db") == "kept"


def test_a_crashed_wal_database_is_folded_in_before_the_rename(home: Path) -> None:
    old = home / "spend.db"
    con = sqlite3.connect(old)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES ('in-wal')")
    con.commit()
    # Simulate a crash: copy the files while the connection (and so the WAL) is still live.
    for suffix in ("", "-wal", "-shm"):
        src = Path(f"{old}{suffix}")
        if src.exists():
            shutil.copy(src, home / f"crash.db{suffix}")
    con.close()
    old.unlink()
    for suffix in ("-wal", "-shm"):
        Path(f"{old}{suffix}").unlink(missing_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = home / f"crash.db{suffix}"
        if src.exists():
            src.rename(Path(f"{old}{suffix}"))

    paths.prepare_home()
    assert paths.db_path() == home / "finance.db"
    assert _value(home / "finance.db") == "in-wal"
    assert not list(home.glob("spend.db*"))


def test_a_database_open_elsewhere_is_not_renamed(home: Path, warnings_logged: list[str]) -> None:
    old = home / "spend.db"
    _make_db(old, wal=True)
    holder = sqlite3.connect(old)  # another process with the database open: WAL sidecars stay
    holder.execute("SELECT * FROM t").fetchall()
    holder.execute("BEGIN IMMEDIATE")
    paths._warned.clear()
    try:
        paths.prepare_home()
        assert paths.db_path() == old
    finally:
        holder.rollback()
        holder.close()
    assert not (home / "finance.db").exists()
    assert any("in use" in m for m in warnings_logged)


def test_finance_db_wins_when_both_exist(home: Path, warnings_logged: list[str]) -> None:
    _make_db(home / "spend.db")
    _make_db(home / "finance.db")
    paths._warned.clear()
    assert paths.db_path() == home / "finance.db"
    assert (home / "spend.db").exists()
    assert any("spend.db" in m for m in warnings_logged)


def test_existing_db_file_never_renames(home: Path) -> None:
    assert paths.existing_db_file(home) == home / "finance.db"
    _make_db(home / "spend.db")
    assert paths.existing_db_file(home) == home / "spend.db"
    assert (home / "spend.db").exists()
    _make_db(home / "finance.db")
    assert paths.existing_db_file(home) == home / "finance.db"


# --- an old server still has the legacy directory's database open ----------------------------


def test_legacy_dir_with_a_database_open_elsewhere_is_not_moved(
    defaults: tuple[Path, Path], warnings_logged: list[str]
) -> None:
    new, legacy = defaults
    legacy.mkdir()
    old = legacy / "spend.db"
    _make_db(old, wal=True)
    server = sqlite3.connect(old)  # stands in for an old `spend-analyzer serve`: WAL sidecars stay
    server.execute("SELECT * FROM t").fetchall()
    assert list(legacy.glob("spend.db-*"))
    try:
        assert paths.prepare_home() == legacy
        assert paths.get_home() == legacy
        assert paths.db_path() == old
    finally:
        server.close()
    # Nothing moved or renamed under the running server; it keeps writing where it expects to.
    assert legacy.is_dir()
    assert not new.exists()
    assert old.exists()
    assert not (legacy / "finance.db").exists()
    assert any("stop the old" in m and str(legacy) in m for m in warnings_logged)


def test_legacy_dir_moves_after_the_old_server_has_stopped(defaults: tuple[Path, Path]) -> None:
    new, legacy = defaults
    legacy.mkdir()
    _make_db(legacy / "spend.db", wal=True)
    server = sqlite3.connect(legacy / "spend.db")
    server.execute("SELECT * FROM t").fetchall()
    assert paths.prepare_home() == legacy
    server.close()  # the last connection closing removes the WAL sidecars

    assert paths.prepare_home() == new
    assert not legacy.exists()
    assert _value(new / "finance.db") == "kept"


def test_a_crashed_runs_leftover_wal_does_not_block_the_dir_move(
    defaults: tuple[Path, Path],
) -> None:
    new, legacy = defaults
    legacy.mkdir()
    old = legacy / "spend.db"
    con = sqlite3.connect(old)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES ('in-wal')")
    con.commit()
    for suffix in ("", "-wal", "-shm"):  # copy while live, as a crash would leave the files
        shutil.copy(Path(f"{old}{suffix}"), legacy / f"crash.db{suffix}")
    con.close()
    for p in legacy.glob("spend.db*"):
        p.unlink()
    for p in sorted(legacy.glob("crash.db*")):
        p.rename(legacy / p.name.replace("crash.db", "spend.db"))

    assert paths.prepare_home() == new
    assert _value(new / "finance.db") == "in-wal"


# --- recorded PDF paths follow a moved data directory ----------------------------------------


def _stored_paths(db_file: Path) -> dict[str, list[str | None]]:
    con = sqlite3.connect(db_file)
    try:
        return {
            table: [r[0] for r in con.execute(f"SELECT stored_path FROM {table} ORDER BY id")]
            for table in ("statements", "tax_documents")
        }
    finally:
        con.close()


def _legacy_install(legacy: Path) -> dict[str, str]:
    """A pre-rename data directory: a migrated ``spend.db`` whose rows record absolute paths into
    this directory, and the PDFs they name."""
    shas = seed_home(legacy)
    (legacy / "finance.db").rename(legacy / "spend.db")
    return shas


def test_prepare_home_rewrites_recorded_pdf_paths_to_the_moved_directory(
    defaults: tuple[Path, Path],
) -> None:
    new, legacy = defaults
    shas = _legacy_install(legacy)
    before = _stored_paths(legacy / "spend.db")
    assert before["statements"] == [str(legacy / "statements" / f"{shas['statement_sha']}.pdf")]

    assert paths.prepare_home() == new

    after = _stored_paths(new / "finance.db")
    assert after["statements"] == [str(new / "statements" / f"{shas['statement_sha']}.pdf")]
    assert after["tax_documents"] == [str(new / "statements" / f"{shas['tax_sha']}.pdf")]
    assert all(Path(p).is_file() for rows in after.values() for p in rows if p)
    assert paths.prepare_home() == new  # idempotent: nothing left to rewrite


def test_rewrite_changes_only_paths_under_the_old_directory(tmp_path: Path) -> None:
    old = tmp_path / "SpendAnalyzer"
    new = tmp_path / "PersonalFinanceAnalyzer"
    db = tmp_path / "x.db"
    con = sqlite3.connect(db)
    for table in ("statements", "tax_documents"):
        con.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, stored_path TEXT)")
        con.executemany(
            f"INSERT INTO {table} (stored_path) VALUES (?)",
            [
                (f"{old}{os.sep}statements/a.pdf",),
                (f"{old}X/statements/sibling.pdf",),  # a sibling directory sharing the prefix
                ("/elsewhere/statements/b.pdf",),
                (None,),
            ],
        )
    con.commit()
    con.close()

    assert paths.rewrite_stored_paths(db, old, new) == 2
    for table_rows in _stored_paths(db).values():
        assert table_rows == [
            f"{new}{os.sep}statements/a.pdf",
            f"{old}X/statements/sibling.pdf",
            "/elsewhere/statements/b.pdf",
            None,
        ]


def test_rewrite_tolerates_a_database_that_lacks_the_tables_or_does_not_exist(
    tmp_path: Path,
) -> None:
    db = tmp_path / "old.db"
    sqlite3.connect(db).close()  # an empty database: no statements / tax_documents tables yet
    assert paths.rewrite_stored_paths(db, tmp_path / "a", tmp_path / "b") == 0
    missing = tmp_path / "missing.db"
    assert paths.rewrite_stored_paths(missing, tmp_path / "a", tmp_path / "b") == 0
    assert not missing.exists()  # never creates an empty database


def test_rewrite_failure_is_logged_and_leaves_the_rows_unchanged(
    tmp_path: Path, warnings_logged: list[str]
) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    db = tmp_path / "x.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE statements (id INTEGER PRIMARY KEY, stored_path TEXT)")
    con.execute(
        "INSERT INTO statements (stored_path) VALUES (?)", (f"{old}{os.sep}statements/a.pdf",)
    )
    con.execute("CREATE TABLE tax_documents (id INTEGER PRIMARY KEY, stored_path TEXT)")
    con.execute(
        "CREATE TRIGGER fail BEFORE UPDATE ON tax_documents BEGIN SELECT RAISE(ABORT, 'no'); END"
    )
    con.execute(
        "INSERT INTO tax_documents (stored_path) VALUES (?)", (f"{old}{os.sep}statements/b.pdf",)
    )
    con.commit()
    con.close()

    assert paths.rewrite_stored_paths(db, old, new) == 0
    # The statements update ran first in the same transaction, and was rolled back with it.
    assert _stored_paths(db)["statements"] == [f"{old}{os.sep}statements/a.pdf"]
    assert any("could not update the recorded PDF paths" in m for m in warnings_logged)


def test_kept_pdf_prefers_the_recorded_path_then_the_current_home(home: Path) -> None:
    paths.ensure_home()
    sha = "ab" * 32
    current = home / "statements" / f"{sha}.pdf"
    current.write_bytes(b"%PDF-")

    assert paths.kept_pdf(None, sha) == current  # no recorded path: where this version keeps it
    assert paths.kept_pdf("/old/machine/statements/x.pdf", sha) == current  # stale record
    recorded = home / "elsewhere.pdf"
    recorded.write_bytes(b"%PDF-")
    assert paths.kept_pdf(str(recorded), sha) == recorded  # a record that still exists wins
    current.unlink()
    assert paths.kept_pdf("/old/machine/statements/x.pdf", sha) is None
