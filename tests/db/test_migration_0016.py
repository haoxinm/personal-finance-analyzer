"""Migration 0016: manual/excluded row columns, statement flip/period columns, and the
`parsed_total_minor` normalization (the total is the sum as extracted again)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command

from personal_finance_analyzer.db.migrate import build_alembic_config

_PREVIOUS = "0015_import_attention"
# `transactions.source` is left out: a database built by 0001 from the current models carries a
# CHECK on it, which SQLite will not let a column drop away from.
_COLUMNS = (
    ("transactions", "excluded"),
    ("statements", "flip_sign"),
    ("statements", "period_corrected"),
    ("statements", "orig_period_start"),
    ("statements", "orig_period_end"),
)


def _columns(db: Path, table: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def _before_0016(tmp_path: Path) -> tuple[str, Path]:
    db = tmp_path / "mig.db"
    url = f"sqlite:///{db}"
    command.upgrade(build_alembic_config(url), _PREVIOUS)
    con = sqlite3.connect(db)
    try:
        for table, column in _COLUMNS:
            con.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        con.execute(
            "INSERT INTO users (id, name, is_default, pii_aliases, created_at) VALUES (1, 'a', 1, '[]', 'x')"
        )
        con.execute(
            "INSERT INTO issuers (id, name, slug, match_terms, auto_confirm, created_at) VALUES (1, 'B', 'b', '[]', 0, 'x')"
        )
        con.execute(
            "INSERT INTO accounts (id, user_id, issuer_id, account_type, display_name, mask, currency, created_at) "
            "VALUES (1, 1, 1, 'credit', 'c', '', 'USD', 'x')"
        )
        con.execute(
            "INSERT INTO statements (id, account_id, file_sha256, original_name, status, "
            "parsed_total_minor, ingested_at) VALUES (1, 1, 'a', 'a.pdf', 'parsed', 2000, 'x')"
        )
        con.execute(
            "INSERT INTO statements (id, account_id, file_sha256, original_name, status, "
            "parsed_total_minor, ingested_at) VALUES (2, 1, 'b', 'b.pdf', 'parsed', 500, 'x')"
        )
        for txn_id, statement_id, amount, orig in (
            (1, 1, 1500, 1000),
            (2, 1, 500, None),
            (3, 2, 500, None),
        ):
            con.execute(
                "INSERT INTO transactions (id, statement_id, account_id, user_id, posted_date, "
                "description_raw, description_clean, merchant_key, amount_minor, orig_amount_minor, "
                "currency, kind, is_spend, needs_review, dedupe_hash, created_at, updated_at) "
                "VALUES (?, ?, 1, 1, '2026-01-01', 'd', 'd', 'd', ?, ?, 'USD', 'purchase', 1, 0, ?, 'x', 'x')",
                (txn_id, statement_id, amount, orig, f"h{txn_id}"),
            )
        con.commit()
    finally:
        con.close()
    return url, db


def _totals(db: Path) -> list[int]:
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute("SELECT parsed_total_minor FROM statements ORDER BY id")]
    finally:
        con.close()


def test_upgrade_adds_the_columns_with_defaults_and_normalizes_the_total(tmp_path: Path) -> None:
    url, db = _before_0016(tmp_path)
    command.upgrade(build_alembic_config(url), "head")
    for table, column in _COLUMNS:
        assert column in _columns(db, table)
    con = sqlite3.connect(db)
    try:
        assert (
            con.execute("SELECT source, excluded FROM transactions").fetchall()
            == [("parsed", 0)] * 3
        )
        assert (
            con.execute(
                "SELECT flip_sign, period_corrected, orig_period_start FROM statements"
            ).fetchall()
            == [(0, 0, None)] * 2
        )
    finally:
        con.close()
    # Statement 1 carried a +500 correction delta; statement 2 had none.
    assert _totals(db) == [1500, 500]


def test_downgrade_restores_the_old_totals_and_upgrade_is_repeatable(tmp_path: Path) -> None:
    url, db = _before_0016(tmp_path)
    config = build_alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, _PREVIOUS)
    assert _totals(db) == [2000, 500]
    for table, column in _COLUMNS:
        assert column not in _columns(db, table)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    assert _totals(db) == [1500, 500]


def test_a_fresh_database_has_the_columns(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    command.upgrade(build_alembic_config(f"sqlite:///{db}"), "head")
    assert "source" in _columns(db, "transactions")
    for table, column in _COLUMNS:
        assert column in _columns(db, table)
