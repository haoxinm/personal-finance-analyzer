"""Migration 0008: `income_items` accepts the W-2 box 12 memo categories; rows survive both ways."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from personal_finance_analyzer.db.migrate import build_alembic_config, upgrade_head
from personal_finance_analyzer.db.session import make_engine_for_path

_PRE = "0007_net_worth_forecast"


def _seed(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, name, is_default, pii_aliases, created_at)"
                " VALUES (1, 'a', 0, '[]', 'now')"
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
        conn.execute(_insert("salary_wages", 100))


def _insert(category: str, amount: int):  # type: ignore[no-untyped-def]
    return text(
        "INSERT INTO income_items (form_id, user_id, tax_year, category, amount_minor, currency,"
        f" source_form, is_memo) VALUES (1, 1, 2024, '{category}', {amount}, 'USD', 'W-2', 0)"
    )


def test_old_constraint_rejects_then_upgrade_accepts_and_keeps_rows(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    url = f"sqlite:///{db}"
    upgrade_head(url)
    engine = make_engine_for_path(db)
    _seed(engine)
    engine.dispose()

    command.downgrade(build_alembic_config(url), _PRE)
    engine = make_engine_for_path(db)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(_insert("retirement_deferral", 5))
    engine.dispose()

    upgrade_head(url)
    engine = make_engine_for_path(db)
    try:
        with engine.begin() as conn:
            conn.execute(_insert("retirement_deferral", 320_000))
            conn.execute(_insert("hsa_employer", 125_000))
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT category, amount_minor FROM income_items ORDER BY id")
            ).all()
            assert [tuple(r) for r in rows] == [
                ("salary_wages", 100),
                ("retirement_deferral", 320_000),
                ("hsa_employer", 125_000),
            ]
            names = {
                r[0]
                for r in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE tbl_name = 'income_items'")
                )
            }
            assert {"ix_income_form", "ix_income_user_year_cat"} <= names
    finally:
        engine.dispose()


def test_downgrade_drops_rows_that_use_the_new_categories(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    url = f"sqlite:///{db}"
    upgrade_head(url)
    engine = make_engine_for_path(db)
    _seed(engine)
    with engine.begin() as conn:
        conn.execute(_insert("retirement_deferral", 5))
    engine.dispose()

    command.downgrade(build_alembic_config(url), _PRE)
    engine = make_engine_for_path(db)
    try:
        with engine.connect() as conn:
            cats = conn.execute(text("SELECT category FROM income_items")).scalars().all()
            assert cats == ["salary_wages"]
    finally:
        engine.dispose()
