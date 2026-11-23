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

**Suggestions.** `GET /transactions/{id}/candidates` (and the opt-in `candidates` / `conflict`
fields of the list, plus its `?conflict=` filter) expose what *each* classification layer would
say about a row (`classify.candidates`), read-only and without any LLM call. Applying one is just
a `PATCH` with that category: a one-time edit, undoable like any other.

**Back to automatic.** A one-time edit is final for every automated pass, so it has an explicit
way out: `POST /transactions/{id}/revert-to-automatic` (and `/transactions/bulk-revert-to-automatic`
for many) drops the edit and re-runs the deterministic cascade for those rows, never the LLM
(`classify.cascade.revert_to_automatic`). The response carries the same undo token as an accept;
`POST /review/undo` puts the edit back.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SessionDep, SettingsDep
from personal_finance_analyzer.api.services import crud, gateway
from personal_finance_analyzer.classify import candidates as suggestions
from personal_finance_analyzer.classify import cascade
from personal_finance_analyzer.db.models import Transaction

#: Earliest date a corrected `posted_date` may take.
_MIN_POSTED_DATE = date(1970, 1, 1)

router = APIRouter(tags=["transactions"])


def _candidates_response(
    candidate_set: suggestions.CandidateSet,
) -> list[schemas.TransactionCandidate]:
    return [
        schemas.TransactionCandidate(
            source=c.source,
            category_key=c.category_key,
            subcategory_key=c.subcategory_key,
            kind=c.kind,
            label=c.label,
            rule_id=c.rule_id,
            is_current=c.is_current,
            is_automatic=c.is_automatic,
        )
        for c in candidate_set.candidates
    ]


def _corrections_response(txn: Transaction) -> schemas.TransactionCorrections | None:
    if (
        txn.orig_posted_date is None
        and txn.orig_amount_minor is None
        and txn.orig_description_clean is None
    ):
        return None
    return schemas.TransactionCorrections(
        posted_date=txn.orig_posted_date,
        amount_minor=txn.orig_amount_minor,
        description_clean=txn.orig_description_clean,
    )


def _transaction_response(
    session: Session,
    txn: Transaction,
    candidate_set: suggestions.CandidateSet | None = None,
    *,
    with_candidates: bool = False,
) -> schemas.Transaction:
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
        corrections=_corrections_response(txn),
        source="manual" if txn.source == "manual" else "parsed",
        excluded=txn.excluded,
        candidates=_candidates_response(candidate_set)
        if candidate_set is not None and with_candidates
        else None,
        conflict=candidate_set.conflict if candidate_set is not None else None,
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
        conflict: bool | None = None,
        excluded: Annotated[
            Literal["hide", "only", "include"],
            Query(
                description="Rows excluded from totals: hidden (default), only those, or "
                "everything."
            ),
        ] = "hide",
        source: Annotated[
            Literal["parsed", "manual"] | None,
            Query(description="Only rows read from a statement, or only those added by hand."),
        ] = None,
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
            conflict=conflict,
            excluded=excluded,
            source=source,
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
    include_candidates: bool = False,
    include_conflict: bool = False,
) -> schemas.TransactionList:
    """One page of transactions. `include_candidates` adds each row's per-layer suggestions
    (`candidates`) and `conflict`; `include_conflict` adds just the `conflict` flag (the grid's
    marker). Both are batch-computed for this page only. `conflict=true|false` (a filter)
    narrows the whole result set to rows with, or without, conflicting suggestions."""
    items, total = crud.list_transactions(
        session, filters, page=page, page_size=page_size, sort=sort
    )
    candidate_sets = (
        suggestions.candidates_for_transactions(session, items)
        if include_candidates or include_conflict
        else {}
    )
    return schemas.TransactionList(
        items=[
            _transaction_response(
                session, t, candidate_sets.get(t.id), with_candidates=include_candidates
            )
            for t in items
        ],
        total=total,
    )


@router.get("/transactions/ids", response_model=schemas.TransactionIds)
def list_transaction_ids(
    session: SessionDep, filters: TransactionFiltersDep, sort: str | None = None
) -> schemas.TransactionIds:
    """Ids of *every* transaction matching the same filters as `GET /transactions` (no page
    cap), for "select all N matching" in the grid."""
    ids = crud.transaction_ids(session, filters, sort=sort)
    return schemas.TransactionIds(ids=ids, total=len(ids))


@router.post("/transactions", response_model=schemas.Transaction, status_code=201)
def create_transaction(
    body: schemas.TransactionCreate, session: SessionDep, settings: SettingsDep
) -> schemas.Transaction:
    """Add a transaction by hand to an *imported* statement (its account and person apply).

    With a category it is a one-time edit; without one the deterministic cascade (your rules,
    built-in rules, the learned cache; never the LLM, nothing is learned from it) files it, and a
    row nothing places lands in `others/uncategorized` flagged for review. A later import or
    re-import that finds the row on the bank's statement absorbs it. Rows added by hand are never
    deleted by a re-import."""
    statement = crud.get_statement(session, body.statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="statement not found")
    if statement.status != "parsed" or statement.account_id is None:
        raise HTTPException(
            status_code=409,
            detail="this statement is not imported yet; finish importing it first",
        )
    if not (_MIN_POSTED_DATE <= body.posted_date <= date.today() + timedelta(days=366)):
        raise HTTPException(
            status_code=400,
            detail="posted_date must be between 1970-01-01 and a year from today",
        )
    if (body.category_key is None) != (body.subcategory_key is None):
        raise HTTPException(
            status_code=400,
            detail="category_key and subcategory_key must be given together",
        )
    if body.category_key is not None and body.subcategory_key is not None:
        category = crud.category_by_key(session, body.category_key)
        if category is None:
            raise HTTPException(status_code=400, detail=f"unknown category {body.category_key!r}")
        if crud.subcategory_by_key(session, category.id, body.subcategory_key) is None:
            raise HTTPException(
                status_code=400, detail=f"unknown subcategory {body.subcategory_key!r}"
            )

    transaction_id = gateway.add_manual_transaction(
        session,
        statement.id,
        posted_date=body.posted_date,
        description=body.description,
        amount_minor=body.amount_minor,
        kind=body.kind.value if body.kind is not None else None,
        notes=body.notes,
    )
    if body.category_key is not None and body.subcategory_key is not None:
        gateway.apply_user_correction(
            session,
            transaction_id,
            category_key=body.category_key,
            subcategory_key=body.subcategory_key,
            kind=body.kind.value if body.kind is not None else None,
            create_rule=False,
        )
    else:
        gateway.classify_deterministic(
            session, [transaction_id], confidence_threshold=settings.llm.confidence_threshold
        )
    session.commit()
    txn = crud.get_transaction(session, transaction_id)
    assert txn is not None
    session.refresh(txn)
    return _transaction_response(session, txn)


@router.delete("/transactions/{id}", status_code=204)
def delete_transaction(
    id: int,  # keep the name "id": the generated frontend path key derives from it
    session: SessionDep,
) -> Response:
    """Delete a row that was added by hand. A row read from a statement cannot be deleted (a
    re-import would bring it back): 409 `parsed_row`; exclude it instead."""
    txn = crud.get_transaction(session, id)
    if txn is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    if not crud.delete_manual_transaction(session, txn):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "parsed_row",
                "message": (
                    "Rows read from the statement can't be deleted (a re-import would bring "
                    "them back). Exclude it instead."
                ),
            },
        )
    session.commit()
    return Response(status_code=204)


@router.post("/transactions/bulk-exclude", response_model=schemas.BulkExcludeResponse)
def bulk_exclude_transactions(
    body: schemas.BulkExcludeRequest, session: SessionDep
) -> schemas.BulkExcludeResponse:
    """Exclude (or, with `excluded=false`, include again) every listed transaction. Returns the
    ids that actually changed: send exactly those back with `excluded` negated to undo."""
    changed = crud.set_excluded(session, body.transaction_ids, body.excluded)
    session.commit()
    return schemas.BulkExcludeResponse(updated=len(changed), changed_ids=changed)


@router.get("/transactions/{id}/candidates", response_model=schemas.TransactionCandidates)
def get_transaction_candidates(
    id: int,  # keep the name "id": the generated frontend path key derives from it
    session: SessionDep,
) -> schemas.TransactionCandidates:
    """What each classification layer (your rules, built-in rules, the issuer's label, the learned
    cache / saved LLM answer) would say about this transaction, highest priority first, and
    whether they disagree. Read-only; never calls the LLM. Click-to-apply is `PATCH
    /transactions/{id}` with the chosen category."""
    txn = crud.get_transaction(session, id)
    if txn is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    candidate_set = suggestions.candidates_for_transaction(session, txn)
    return schemas.TransactionCandidates(
        transaction_id=txn.id,
        candidates=_candidates_response(candidate_set),
        conflict=candidate_set.conflict,
    )


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

    if body.posted_date is not None and not (
        _MIN_POSTED_DATE <= body.posted_date <= date.today() + timedelta(days=366)
    ):
        raise HTTPException(
            status_code=400,
            detail="posted_date must be between 1970-01-01 and a year from today",
        )
    given = {
        "posted_date": body.posted_date,
        "amount_minor": body.amount_minor,
        "description_clean": body.description_clean,
    }
    clash = sorted(f for f in set(body.revert_fields) if given[f] is not None)
    if clash:
        raise HTTPException(
            status_code=400, detail=f"cannot both set and revert: {', '.join(clash)}"
        )

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
        session.flush()
        session.refresh(txn)

    remaining_kind = (
        body.kind if body.category_key is None and body.subcategory_key is None else None
    )
    if remaining_kind is not None or body.notes is not None:
        crud.update_transaction_fields(session, txn, kind=remaining_kind, notes=body.notes)

    if body.excluded is not None:
        crud.set_excluded(session, [transaction_id], body.excluded)

    if body.revert_fields or any(v is not None for v in given.values()):
        crud.apply_field_corrections(
            session,
            txn,
            posted_date=body.posted_date.isoformat() if body.posted_date is not None else None,
            description_clean=body.description_clean,
            amount_minor=body.amount_minor,
            revert_fields=body.revert_fields,
        )

    session.commit()  # one transaction: category + kind/notes + corrections land together
    return _transaction_response(session, txn)


@router.post(
    "/transactions/{id}/revert-to-automatic", response_model=schemas.RevertToAutomaticResponse
)
def revert_transaction_to_automatic(
    id: int,  # keep the name "id": the generated frontend path key derives from it
    session: SessionDep,
    settings: SettingsDep,
) -> schemas.RevertToAutomaticResponse:
    """Drop this transaction's one-time edit and re-run the deterministic cascade for it (your
    rules, built-in rules, the issuer's label, the learned cache; uncategorized and flagged for
    review if none answers). Never calls the LLM. 409 if the row is not currently edited by hand.
    The undo token goes to `POST /review/undo` to restore the edit."""
    txn = crud.get_transaction(session, id)
    if txn is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    result = cascade.revert_to_automatic(
        session, [id], confidence_threshold=settings.llm.confidence_threshold
    )
    if not result.reverted_ids:
        session.rollback()
        raise HTTPException(status_code=409, detail="transaction has no one-time edit to revert")
    session.commit()
    session.refresh(txn)
    return schemas.RevertToAutomaticResponse(
        transaction=_transaction_response(session, txn),
        undo=schemas.AcceptUndo.model_validate(result.undo, from_attributes=True),
    )


@router.post("/transactions/bulk-revert-to-automatic", response_model=schemas.BulkRevertResponse)
def bulk_revert_to_automatic(
    body: schemas.BulkRevertRequest, session: SessionDep, settings: SettingsDep
) -> schemas.BulkRevertResponse:
    """`revert-to-automatic` for many rows. Rows that are not edited by hand, or no longer exist,
    are skipped and counted; the rest go back to automatic classification (no LLM call)."""
    result = cascade.revert_to_automatic(
        session,
        body.transaction_ids,
        confidence_threshold=settings.llm.confidence_threshold,
    )
    session.commit()
    return schemas.BulkRevertResponse(
        reverted=len(result.reverted_ids),
        skipped=len(result.skipped_ids),
        undo=schemas.AcceptUndo.model_validate(result.undo, from_attributes=True),
    )


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
