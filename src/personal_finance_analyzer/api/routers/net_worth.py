"""`/api/net-worth/*`. No SQL here: delegates to `api/services/net_worth.py` and
`analytics/net_worth_query.py`. Balances are local only (never exported, never sent to an LLM)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from personal_finance_analyzer.analytics.net_worth_query import NetWorthQuery, run_net_worth_query
from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SessionDep
from personal_finance_analyzer.api.services import net_worth as service
from personal_finance_analyzer.core.net_worth import BalanceCategory

router = APIRouter(tags=["net-worth"])


@router.get("/net-worth/entries", response_model=list[schemas.BalanceEntry])
def list_entries(
    session: SessionDep,
    user_id: int | None = None,
    category: BalanceCategory | None = None,
    year: int | None = None,
    account_name: str | None = None,
) -> list[schemas.BalanceEntry]:
    try:
        rows = service.list_entries(
            session, user_id=user_id, category=category, year=year, account_name=account_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return [schemas.BalanceEntry.model_validate(r) for r in rows]


@router.post("/net-worth/entries", response_model=schemas.BalanceEntry, status_code=201)
def upsert_entry(
    body: schemas.BalanceEntryUpsert, response: Response, session: SessionDep
) -> schemas.BalanceEntry:
    if not service.user_exists(session, body.user_id):
        raise HTTPException(status_code=404, detail="user not found")
    try:
        entry, created = service.upsert_entry(session, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    session.commit()
    if not created:
        response.status_code = 200
    return schemas.BalanceEntry.model_validate(entry)


@router.patch("/net-worth/entries/{id}", response_model=schemas.BalanceEntry)
def patch_entry(
    id: int, body: schemas.BalanceEntryPatch, session: SessionDep
) -> schemas.BalanceEntry:
    entry = service.get_entry(session, id)
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    service.patch_entry(
        session,
        entry,
        balance_minor=body.balance_minor,
        note=body.note,
        set_note="note" in body.model_fields_set,
    )
    session.commit()
    return schemas.BalanceEntry.model_validate(entry)


@router.delete("/net-worth/entries/{id}", status_code=204)
def delete_entry(id: int, session: SessionDep) -> None:
    entry = service.get_entry(session, id)
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    service.delete_entry(session, entry)
    session.commit()


@router.get("/net-worth/summary", response_model=schemas.NetWorthSummary)
def summary(
    session: SessionDep,
    user_id: int | None = None,
    currency: str = "USD",
    period: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    carry_forward: Annotated[bool, Query()] = True,
) -> schemas.NetWorthSummary:
    try:
        result = run_net_worth_query(
            session,
            NetWorthQuery(
                user_id=user_id,
                currency=currency,
                period=period,
                period_from=period_from,
                period_to=period_to,
                carry_forward=carry_forward,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return schemas.NetWorthSummary.model_validate(result)
