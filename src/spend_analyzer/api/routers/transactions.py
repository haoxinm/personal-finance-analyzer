"""`/api/transactions` (§3.12).

An edit here is a **one-time patch**: it changes the one transaction (or, for the bulk endpoint,
each listed transaction) and marks it `classified_by='user'`, after which no rule, re-run, cache or
LLM pass ever changes it (I6). It never writes a `merchant_map` row or a rule, so it cannot
affect any other or future transaction. The only way to make an edit stick to a merchant is the
explicit `create_rule` flag on `PATCH /transactions/{id}` ("always categorize this merchant this
way"), which saves an exact user rule in addition to patching the transaction.

A category/subcategory change goes through `classify.cascade.apply_user_correction` (§3.12a,
P2-B) via `api/services/gateway.py`; a kind-only change goes through
`crud.update_transaction_fields`, which marks the row the same way; notes never lock a row.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from spend_analyzer.api import schemas
from spend_analyzer.api.deps import SessionDep
from spend_analyzer.api.services import crud, gateway
from spend_analyzer.db.models import Transaction

router = APIRouter(tags=["transactions"])


def _transaction_response(session: Session, txn: Transaction) -> schemas.Transaction:
    return schemas.Transaction(
        id=txn.id,
        statement_id=txn.statement_id,
        account_id=txn.account_id,
        posted_date=txn.posted_date,
        transaction_date=txn.transaction_date,
        description_raw=txn.description_raw,
        description_clean=txn.description_clean,
        amount_minor=txn.amount_minor,
        currency=txn.currency,
        kind=txn.kind,
        category_key=crud.category_key_of(session, txn),
        subcategory_key=crud.subcategory_key_of(session, txn),
        merchant_key=txn.merchant_key,
        notes=txn.notes,
        needs_review=txn.needs_review,
    )


def _filters_dependency(
    *, include_non_spend_default: bool, currency_default: str | None
) -> Callable[..., crud.TransactionFilters]:
    """Build the FastAPI dependency that parses the shared transaction filter query parameters.
    The list and ids endpoints use the spend-relevant defaults; the export defaults to
    everything (all kinds, all currencies) unless a filter narrows it."""

    def dependency(
        user_ids: Annotated[list[int] | None, Query()] = None,
        account_ids: Annotated[list[int] | None, Query()] = None,
        statement_id: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        category_keys: Annotated[list[str] | None, Query()] = None,
        subcategory_keys: Annotated[list[str] | None, Query()] = None,
        amount_min_minor: int | None = None,
        amount_max_minor: int | None = None,
        kinds: Annotated[list[schemas.Kind] | None, Query()] = None,
        include_non_spend: bool = include_non_spend_default,
        needs_review: bool | None = None,
        uncategorized: bool | None = None,
        search: str | None = None,
        currency: str | None = currency_default,
    ) -> crud.TransactionFilters:
        return crud.TransactionFilters(
            user_ids=user_ids,
            account_ids=account_ids,
            statement_id=statement_id,
            date_from=date_from,
            date_to=date_to,
            category_keys=category_keys,
            subcategory_keys=subcategory_keys,
            amount_min_minor=amount_min_minor,
            amount_max_minor=amount_max_minor,
            kinds=[k.value for k in kinds] if kinds is not None else None,
            include_non_spend=include_non_spend,
            needs_review=needs_review,
            uncategorized=uncategorized,
            search=search,
            currency=currency,
        )

    return dependency


#: `GET /transactions` and `GET /transactions/ids` parse their filters the same way.
TransactionFiltersDep = Annotated[
    crud.TransactionFilters,
    Depends(_filters_dependency(include_non_spend_default=False, currency_default="USD")),
]
#: `GET /export`: same parameters, but unfiltered means *everything*.
ExportFiltersDep = Annotated[
    crud.TransactionFilters,
    Depends(_filters_dependency(include_non_spend_default=True, currency_default=None)),
]


@router.get("/transactions", response_model=schemas.TransactionList)
def list_transactions(
    session: SessionDep,
    filters: TransactionFiltersDep,
    page: int = 1,
    page_size: int = 50,
    sort: str | None = None,
) -> schemas.TransactionList:
    items, total = crud.list_transactions(
        session, filters, page=page, page_size=page_size, sort=sort
    )
    return schemas.TransactionList(
        items=[_transaction_response(session, t) for t in items], total=total
    )


@router.get("/transactions/ids", response_model=schemas.TransactionIds)
def list_transaction_ids(
    session: SessionDep, filters: TransactionFiltersDep, sort: str | None = None
) -> schemas.TransactionIds:
    """Ids of *every* transaction matching the same filters as `GET /transactions` (no page
    cap), for "select all N matching" in the grid."""
    ids = crud.transaction_ids(session, filters, sort=sort)
    return schemas.TransactionIds(ids=ids, total=len(ids))


@router.patch("/transactions/{id}", response_model=schemas.Transaction)
def patch_transaction(
    id: int,  # the frontend's generated path key (`/transactions/{id}`) is derived from this
    # exact parameter name (openapi-typescript); keep it "id" to match.
    body: schemas.TransactionPatch,
    session: SessionDep,
) -> schemas.Transaction:
    transaction_id = id
    txn = crud.get_transaction(session, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="transaction not found")

    if body.category_key is not None or body.subcategory_key is not None:
        category_key = body.category_key or crud.category_key_of(session, txn)
        subcategory_key = body.subcategory_key or crud.subcategory_key_of(session, txn)
        if category_key is None or subcategory_key is None:
            raise HTTPException(
                status_code=400,
                detail="category_key and subcategory_key are both required to set either",
            )
        gateway.apply_user_correction(
            session,
            transaction_id,
            category_key=category_key,
            subcategory_key=subcategory_key,
            kind=body.kind.value if body.kind is not None else None,
            create_rule=body.create_rule,
        )
        session.commit()
        session.refresh(txn)

    remaining_kind = (
        body.kind if body.category_key is None and body.subcategory_key is None else None
    )
    if remaining_kind is not None or body.notes is not None:
        crud.update_transaction_fields(session, txn, kind=remaining_kind, notes=body.notes)
        session.commit()

    return _transaction_response(session, txn)


@router.post("/transactions/bulk-update", response_model=schemas.BulkUpdateResponse)
def bulk_update_transactions(
    body: schemas.BulkUpdateRequest, session: SessionDep
) -> schemas.BulkUpdateResponse:
    """Apply one label and/or kind to `transaction_ids`. `updated` counts only rows actually
    changed: ids that no longer exist, and a request with nothing to apply, count for nothing —
    compare it with `len(transaction_ids)` to spot a partial update."""
    if (body.category_key is None) != (body.subcategory_key is None):
        raise HTTPException(
            status_code=400, detail="category_key and subcategory_key must be given together"
        )
    if body.category_key is not None and body.subcategory_key is not None:
        category = crud.category_by_key(session, body.category_key)
        if category is None:
            raise HTTPException(status_code=400, detail=f"unknown category {body.category_key!r}")
        if crud.subcategory_by_key(session, category.id, body.subcategory_key) is None:
            raise HTTPException(
                status_code=400, detail=f"unknown subcategory {body.subcategory_key!r}"
            )

    updated = 0
    for transaction_id in body.transaction_ids:
        txn = crud.get_transaction(session, transaction_id)
        if txn is None:
            continue
        if body.category_key is not None and body.subcategory_key is not None:
            gateway.apply_user_correction(
                session,
                transaction_id,
                category_key=body.category_key,
                subcategory_key=body.subcategory_key,
                kind=body.kind.value if body.kind is not None else None,
                create_rule=False,
            )
        elif body.kind is not None:
            crud.update_transaction_fields(session, txn, kind=body.kind, notes=None)
        else:
            continue
        updated += 1
    session.commit()
    return schemas.BulkUpdateResponse(updated=updated)
