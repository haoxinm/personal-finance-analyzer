"""Migration 0006: provenance columns are added to existing tax tables with their defaults."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from sqlalchemy import inspect, text

from personal_finance_analyzer.db.migrate import build_alembic_config, upgrade_head
from personal_finance_analyzer.db.session import make_engine_for_path

_NEW = {
    "tax_forms": ("extraction_method", "is_user_edited"),
    "income_items": ("origin",),
    "tax_items": ("origin",),
}


def _columns(db: Path, table: str) -> set[str]:
    eng = make_engine_for_path(db)
    try:
        return {c["name"] for c in inspect(eng).get_columns(table)}
    finally:
        eng.dispose()


def test_upgrade_from_0005_keeps_rows_and_applies_defaults(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    url = f"sqlite:///{db}"
    upgrade_head(url)
    eng = make_engine_for_path(db)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, name, is_default, pii_aliases, created_at) VALUES (1, 'a', 0, '[]', 'now')"
            )
        )
        # Simulate a 0005-era database: no provenance columns.
        for table, cols in _NEW.items():
            for col in cols:
                conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {col}"))
        conn.execute(
            text(
                "INSERT INTO tax_documents (id, user_id, file_sha256, original_name, tax_year,"
                " status, ingested_at) VALUES (1, 1, 'h', 'w2.pdf', 2024, 'parsed', 'now')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tax_forms (id, document_id, user_id, tax_year, form_type, role,"
                " is_corrected, fingerprint, created_at)"
                " VALUES (1, 1, 1, 2024, 'w2', 'source', 0, 'fp', 'now')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO income_items (form_id, user_id, tax_year, category, amount_minor,"
                " currency, source_form, is_memo) VALUES (1, 1, 2024, 'salary_wages', 100, 'USD',"
                " 'W-2', 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tax_items (form_id, user_id, tax_year, category, amount_minor,"
                " currency, source_form, is_estimate) VALUES (1, 1, 2024,"
                " 'federal_income_tax_withheld', 10, 'USD', 'W-2', 0)"
            )
        )
        conn.execute(text("UPDATE alembic_version SET version_num='0005_balance_entries'"))
    eng.dispose()

    upgrade_head(url)

    eng = make_engine_for_path(db)
    try:
        with eng.connect() as conn:
            form = conn.execute(
                text("SELECT extraction_method, is_user_edited FROM tax_forms")
            ).one()
            assert tuple(form) == ("parser", 0)
            assert conn.execute(text("SELECT origin FROM income_items")).scalar_one() == "parser"
            assert conn.execute(text("SELECT origin FROM tax_items")).scalar_one() == "parser"
    finally:
        eng.dispose()


def test_downgrade_removes_columns_and_keeps_rows(tmp_path: Path) -> None:
    db = tmp_path / "down.db"
    url = f"sqlite:///{db}"
    upgrade_head(url)
    eng = make_engine_for_path(db)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, name, is_default, pii_aliases, created_at) VALUES (1, 'a', 0, '[]', 'now')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tax_documents (id, user_id, file_sha256, original_name, status,"
                " ingested_at) VALUES (1, 1, 'h', 'x', 'parsed', 'now')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tax_forms (id, document_id, user_id, tax_year, form_type, role,"
                " is_corrected, fingerprint, created_at)"
                " VALUES (1, 1, 1, 2024, 'w2', 'source', 0, 'fp', 'now')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO income_items (form_id, user_id, tax_year, category, amount_minor,"
                " currency, source_form, is_memo) VALUES (1, 1, 2024, 'salary_wages', 100, 'USD',"
                " 'W-2', 0)"
            )
        )
    eng.dispose()

    command.downgrade(build_alembic_config(url), "0005_balance_entries")

    for table, cols in _NEW.items():
        assert not set(cols) & _columns(db, table)
    eng = make_engine_for_path(db)
    try:
        with eng.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM income_items")).scalar_one() == 1
    finally:
        eng.dispose()
