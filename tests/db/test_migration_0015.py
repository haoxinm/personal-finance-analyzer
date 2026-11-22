"""Migration 0015: `statements.parsed_row_count`, `issuers.default_parser_id`, `jobs.error_code`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command

from personal_finance_analyzer.db.migrate import build_alembic_config

_PREVIOUS = "0014_dismiss_and_corrections"
_COLUMNS = (
    ("statements", "parsed_row_count"),
    ("issuers", "default_parser_id"),
    ("jobs", "error_code"),
)


def _columns(db: Path, table: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def _database_before_0015(tmp_path: Path) -> tuple[str, Path]:
    """A database as 0014 left it (new columns dropped again, rows inserted)."""
    db = tmp_path / "mig.db"
    url = f"sqlite:///{db}"
    command.upgrade(build_alembic_config(url), _PREVIOUS)
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO issuers (name, slug, match_terms, auto_confirm, created_at) "
            "VALUES ('Bank', 'bank', '[]', 0, 'x')"
        )
        con.execute(
            "INSERT INTO jobs (id, kind, status, total, done, cost_usd, payload_json, "
            "created_at, updated_at) VALUES ('j', 'import', 'done', 1, 1, 0, '{}', 'x', 'x')"
        )
        for table, column in _COLUMNS:
            con.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        con.commit()
    finally:
        con.close()
    return url, db


def test_upgrade_adds_nullable_columns_and_keeps_rows(tmp_path: Path) -> None:
    url, db = _database_before_0015(tmp_path)
    for table, column in _COLUMNS:
        assert column not in _columns(db, table)

    command.upgrade(build_alembic_config(url), "head")

    for table, column in _COLUMNS:
        assert column in _columns(db, table)
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT slug, default_parser_id FROM issuers").fetchall() == [
            ("bank", None)
        ]
        assert con.execute("SELECT id, error_code FROM jobs").fetchall() == [("j", None)]
    finally:
        con.close()


def test_upgrade_is_idempotent_and_downgrade_removes_the_columns(tmp_path: Path) -> None:
    url, db = _database_before_0015(tmp_path)
    config = build_alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, _PREVIOUS)
    for table, column in _COLUMNS:
        assert column not in _columns(db, table)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    for table, column in _COLUMNS:
        assert column in _columns(db, table)


def test_a_fresh_database_already_has_the_columns(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    command.upgrade(build_alembic_config(f"sqlite:///{db}"), "head")
    for table, column in _COLUMNS:
        assert column in _columns(db, table)
