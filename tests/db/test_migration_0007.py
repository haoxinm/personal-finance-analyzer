"""Migration 0007: `net_worth_history` and `forecast_presets` are added to an older database."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from personal_finance_analyzer.db.migrate import build_alembic_config, upgrade_head
from personal_finance_analyzer.db.session import make_engine_for_path

_TABLES = {"net_worth_history", "forecast_presets"}
_USER = "INSERT INTO users (id, name, is_default, pii_aliases, created_at) VALUES (1, 'a', 0, '[]', 'now')"


def _tables(db: Path) -> set[str]:
    eng = make_engine_for_path(db)
    try:
        return set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def test_downgrade_drops_new_tables_and_keeps_others(tmp_path: Path) -> None:
    db = tmp_path / "down.db"
    url = f"sqlite:///{db}"
    upgrade_head(url)
    assert _tables(db) >= _TABLES
    eng = make_engine_for_path(db)
    with eng.begin() as conn:
        conn.execute(text(_USER))
    eng.dispose()

    command.downgrade(build_alembic_config(url), "0006_tax_provenance")

    tables = _tables(db)
    assert not _TABLES & tables
    assert {"users", "balance_entries"} <= tables


def test_upgrade_from_0006_creates_tables_with_constraints(tmp_path: Path) -> None:
    db = tmp_path / "up.db"
    url = f"sqlite:///{db}"
    upgrade_head(url)
    command.downgrade(build_alembic_config(url), "0006_tax_provenance")
    assert not _TABLES & _tables(db)

    upgrade_head(url)

    assert _tables(db) >= _TABLES
    eng = make_engine_for_path(db)
    try:
        with eng.begin() as conn:
            conn.execute(text(_USER))
            conn.execute(
                text(
                    "INSERT INTO net_worth_history (user_id, year, amount_minor, created_at,"
                    " updated_at) VALUES (1, 2020, -5, 'now', 'now')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO forecast_presets (user_id, name, params, created_at, updated_at)"
                    " VALUES (NULL, 'Shared', '{}', 'now', 'now')"
                )
            )
        # Unique (user, year), and a unique shared name even though user_id is NULL.
        with pytest.raises(IntegrityError), eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO net_worth_history (user_id, year, amount_minor, created_at,"
                    " updated_at) VALUES (1, 2020, 1, 'now', 'now')"
                )
            )
        with pytest.raises(IntegrityError), eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO forecast_presets (user_id, name, params, created_at, updated_at)"
                    " VALUES (NULL, 'Shared', '{}', 'now', 'now')"
                )
            )
    finally:
        eng.dispose()
