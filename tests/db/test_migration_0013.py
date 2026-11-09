"""Migration 0013: `watch_files` and the `pdf_in_place` flag; existing rows are the app's own copies."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command

from personal_finance_analyzer.db.migrate import build_alembic_config

_PREVIOUS = "0012_drop_unplaced_merchant_cache"


def _columns(db: Path, table: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def _database_before_0013(tmp_path: Path) -> tuple[str, Path]:
    """A database as 0012 left it: ``0001_initial`` builds the schema from the *current* models,
    so the new columns and table are dropped again to look like an existing install."""
    db = tmp_path / "mig.db"
    url = f"sqlite:///{db}"
    command.upgrade(build_alembic_config(url), _PREVIOUS)
    con = sqlite3.connect(db)
    try:
        for table in ("statements", "tax_documents"):
            con.execute(f"ALTER TABLE {table} DROP COLUMN pdf_in_place")
        con.execute("DROP TABLE watch_files")
        con.execute(
            "INSERT INTO statements (file_sha256, original_name, status, stored_path, ingested_at) "
            "VALUES (?, 'old.pdf', 'parsed', 'Alex/Statements/Bank/old.pdf', '2026-01-01T00:00:00')",
            ("a" * 64,),
        )
        con.commit()
    finally:
        con.close()
    return url, db


def test_existing_rows_are_the_apps_own_copies_and_nothing_else_changes(tmp_path: Path) -> None:
    url, db = _database_before_0013(tmp_path)
    assert "pdf_in_place" not in _columns(db, "statements")

    command.upgrade(build_alembic_config(url), "head")

    for table in ("statements", "tax_documents"):
        assert "pdf_in_place" in _columns(db, table)
    assert _columns(db, "watch_files")
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT pdf_in_place, stored_path FROM statements").fetchall() == [
            (0, "Alex/Statements/Bank/old.pdf")
        ]
    finally:
        con.close()


def test_upgrade_is_idempotent_and_downgrade_removes_the_flag(tmp_path: Path) -> None:
    url, db = _database_before_0013(tmp_path)
    config = build_alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, _PREVIOUS)
    for table in ("statements", "tax_documents"):
        assert "pdf_in_place" not in _columns(db, table)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    assert "pdf_in_place" in _columns(db, "statements")


def test_a_fresh_database_already_has_the_flag(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    command.upgrade(build_alembic_config(f"sqlite:///{db}"), "head")
    assert "pdf_in_place" in _columns(db, "statements")
    assert "pdf_in_place" in _columns(db, "tax_documents")
