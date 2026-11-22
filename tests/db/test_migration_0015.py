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


def test_upgrade_backfills_the_row_count_of_statements_that_have_rows(tmp_path: Path) -> None:
    url, db = _database_before_0015(tmp_path)
    con = sqlite3.connect(db)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(statements)")]
        base = {
            "file_sha256": "x",
            "original_name": "a.pdf",
            "page_count": 1,
            "status": "parsed",
            "ingested_at": "x",
        }
        for sha, count in (("a", 3), ("b", 0)):
            row = {**base, "file_sha256": sha, "txn_count": count}
            row = {k: v for k, v in row.items() if k in cols}
            con.execute(
                f"INSERT INTO statements ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()),
            )
        con.commit()
    finally:
        con.close()
    command.upgrade(build_alembic_config(url), "head")
    con = sqlite3.connect(db)
    try:
        assert con.execute(
            "SELECT file_sha256, parsed_row_count FROM statements ORDER BY file_sha256"
        ).fetchall() == [("a", 3), ("b", None)]
    finally:
        con.close()
