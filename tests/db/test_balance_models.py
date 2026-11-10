"""balance_entries: migration, CHECK/UNIQUE constraints, cascade delete."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.migrate import upgrade_head
from personal_finance_analyzer.db.models import BalanceEntry, Base, User
from personal_finance_analyzer.db.session import make_engine_for_path


def _user(session: Session) -> User:
    user = User(name="Alex")
    session.add(user)
    session.flush()
    return user


def _entry(user: User, **kw: object) -> BalanceEntry:
    base: dict[str, object] = {
        "user_id": user.id,
        "category": "checking",
        "account_name": "Main",
        "year": 2025,
        "month": 3,
        "balance_minor": 100,
    }
    base.update(kw)
    return BalanceEntry(**base)


def test_fresh_upgrade_creates_table(engine: Engine) -> None:
    assert "balance_entries" in set(inspect(engine).get_table_names())


def test_legacy_upgrade_path_recreates_table(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    url = f"sqlite:///{db}"
    upgrade_head(url)
    eng = make_engine_for_path(db)
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE balance_entries"))
        conn.execute(text("UPDATE alembic_version SET version_num='0004_tax_documents'"))
    eng.dispose()
    upgrade_head(url)
    upgrade_head(url)  # idempotent
    eng = make_engine_for_path(db)
    insp = inspect(eng)
    assert "balance_entries" in insp.get_table_names()
    expected = {ix.name for ix in Base.metadata.tables["balance_entries"].indexes}
    assert {ix["name"] for ix in insp.get_indexes("balance_entries")} >= expected
    eng.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"category": "cash"},
        {"month": 0},
        {"month": 13},
        {"balance_minor": -1},
        {"account_name": "   "},
        {"year": 1800},
    ],
)
def test_check_constraints(session: Session, overrides: dict[str, object]) -> None:
    session.add(_entry(_user(session), **overrides))
    with pytest.raises(IntegrityError):
        session.flush()


def test_unique_period_key(session: Session) -> None:
    user = _user(session)
    session.add(_entry(user))
    session.flush()
    session.add(_entry(user, balance_minor=5))
    with pytest.raises(IntegrityError):
        session.flush()


def test_user_delete_cascades(session: Session) -> None:
    user = _user(session)
    session.add(_entry(user))
    session.flush()
    session.delete(user)
    session.flush()
    assert session.scalar(select(BalanceEntry)) is None
