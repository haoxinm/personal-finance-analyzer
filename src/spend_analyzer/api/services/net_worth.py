"""Balance-entry CRUD for net worth (local only; no LLM/HTTP imports, no commit here)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from spend_analyzer.api import schemas
from spend_analyzer.core.net_worth import normalize_account_name
from spend_analyzer.db.models import BalanceEntry, User


def user_exists(session: Session, user_id: int) -> bool:
    return session.get(User, user_id) is not None


def get_entry(session: Session, entry_id: int) -> BalanceEntry | None:
    return session.get(BalanceEntry, entry_id)


def list_entries(
    session: Session,
    *,
    user_id: int | None = None,
    category: str | None = None,
    year: int | None = None,
    account_name: str | None = None,
) -> list[BalanceEntry]:
    stmt = select(BalanceEntry)
    if user_id is not None:
        stmt = stmt.where(BalanceEntry.user_id == user_id)
    if category is not None:
        stmt = stmt.where(BalanceEntry.category == category)
    if year is not None:
        stmt = stmt.where(BalanceEntry.year == year)
    if account_name is not None:
        stmt = stmt.where(
            func.lower(BalanceEntry.account_name) == normalize_account_name(account_name).lower()
        )
    stmt = stmt.order_by(
        BalanceEntry.year.desc(),
        BalanceEntry.month.desc(),
        BalanceEntry.category,
        BalanceEntry.account_name,
    )
    return list(session.scalars(stmt))


def upsert_entry(session: Session, body: schemas.BalanceEntryUpsert) -> tuple[BalanceEntry, bool]:
    """Insert or update the (user, category, account, year, month) row. The account name is
    canonicalised: any existing row of the same user and category, in *any* month, whose name
    matches case-insensitively keeps its spelling (the newest one wins), so "chase" typed in March
    continues the account "Chase" entered in January instead of starting a second account."""
    name = normalize_account_name(body.account_name)
    known = session.scalar(
        select(BalanceEntry.account_name)
        .where(
            BalanceEntry.user_id == body.user_id,
            BalanceEntry.category == body.category,
            func.lower(BalanceEntry.account_name) == name.lower(),
        )
        .order_by(BalanceEntry.year.desc(), BalanceEntry.month.desc())
        .limit(1)
    )
    if known is not None:
        name = known
    existing = session.scalar(
        select(BalanceEntry).where(
            BalanceEntry.user_id == body.user_id,
            BalanceEntry.category == body.category,
            BalanceEntry.year == body.year,
            BalanceEntry.month == body.month,
            func.lower(BalanceEntry.account_name) == name.lower(),
        )
    )
    if existing is not None:
        existing.balance_minor = body.balance_minor
        existing.currency = body.currency
        existing.note = body.note
        session.flush()
        return existing, False
    entry = BalanceEntry(
        user_id=body.user_id,
        category=body.category,
        account_name=name,
        year=body.year,
        month=body.month,
        balance_minor=body.balance_minor,
        currency=body.currency,
        note=body.note,
    )
    session.add(entry)
    session.flush()
    return entry, True


def patch_entry(
    session: Session,
    entry: BalanceEntry,
    *,
    balance_minor: int | None,
    note: str | None,
    set_note: bool,
) -> BalanceEntry:
    if balance_minor is not None:
        entry.balance_minor = balance_minor
    if set_note:
        entry.note = note
    session.flush()
    return entry


def delete_entry(session: Session, entry: BalanceEntry) -> None:
    session.delete(entry)
    session.flush()
