"""Migration 0014: `watch_files.dismissed_at` and the `orig_*` correction columns."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command

from personal_finance_analyzer.db.migrate import build_alembic_config

_PREVIOUS = "0013_watch_folder"
_TXN_COLUMNS = ("orig_posted_date", "orig_amount_minor", "orig_description_clean")


def _columns(db: Path, table: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def _database_before_0014(tmp_path: Path) -> tuple[str, Path]:
    """A database as 0013 left it (new columns dropped again, rows inserted)."""
    db = tmp_path / "mig.db"
    url = f"sqlite:///{db}"
    command.upgrade(build_alembic_config(url), _PREVIOUS)
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO watch_files (path, size_bytes, mtime_ns, outcome, first_seen, last_seen) "
            "VALUES ('a.pdf', 1, 2, 'failed', 'x', 'x')"
        )
        con.execute("ALTER TABLE watch_files DROP COLUMN dismissed_at")
        for column in _TXN_COLUMNS:
            con.execute(f"ALTER TABLE transactions DROP COLUMN {column}")
        con.commit()
    finally:
        con.close()
    return url, db


def test_upgrade_adds_nullable_columns_and_keeps_rows(tmp_path: Path) -> None:
    url, db = _database_before_0014(tmp_path)
    assert "dismissed_at" not in _columns(db, "watch_files")

    command.upgrade(build_alembic_config(url), "head")

    assert "dismissed_at" in _columns(db, "watch_files")
    for column in _TXN_COLUMNS:
        assert column in _columns(db, "transactions")
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT path, outcome, dismissed_at FROM watch_files").fetchall() == [
            ("a.pdf", "failed", None)
        ]
    finally:
        con.close()


def test_upgrade_is_idempotent_and_downgrade_removes_the_columns(tmp_path: Path) -> None:
    url, db = _database_before_0014(tmp_path)
    config = build_alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, _PREVIOUS)
    assert "dismissed_at" not in _columns(db, "watch_files")
    for column in _TXN_COLUMNS:
        assert column not in _columns(db, "transactions")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    assert "dismissed_at" in _columns(db, "watch_files")
    assert "orig_amount_minor" in _columns(db, "transactions")


def test_a_fresh_database_already_has_the_columns(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    command.upgrade(build_alembic_config(f"sqlite:///{db}"), "head")
    assert "dismissed_at" in _columns(db, "watch_files")
    assert set(_TXN_COLUMNS) <= set(_columns(db, "transactions"))
