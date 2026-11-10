"""`/api/accounts` (§3.12). No SQL here — delegates to `api/services/crud.py`."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SessionDep
from personal_finance_analyzer.api.services import crud

router = APIRouter(tags=["accounts"])


@router.get("/accounts", response_model=list[schemas.Account])
def list_accounts(session: SessionDep) -> list[schemas.Account]:
    return [schemas.Account.model_validate(a) for a in crud.list_accounts(session)]


@router.post("/accounts", response_model=schemas.Account, status_code=201)
def create_account(body: schemas.AccountCreate, session: SessionDep) -> schemas.Account:
    account = crud.create_account(
        session,
        user_id=body.user_id,
        issuer_id=body.issuer_id,
        account_type=body.account_type,
        mask=body.mask,
        currency=body.currency,
    )
    session.commit()
    return schemas.Account.model_validate(account)


@router.patch("/accounts/{account_id}", response_model=schemas.Account)
def update_account(
    account_id: int, body: schemas.AccountUpdate, session: SessionDep
) -> schemas.Account:
    account = crud.get_account(session, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    account = crud.update_account(
        session,
        account,
        issuer_id=body.issuer_id,
        account_type=body.account_type,
        mask=body.mask,
        currency=body.currency,
    )
    session.commit()
    return schemas.Account.model_validate(account)
