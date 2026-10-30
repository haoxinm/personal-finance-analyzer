"""`/api/review/*`: the Review queue's accept and undo.

Accepting is a one-time edit of that transaction (`classify.cascade.apply_user_correction`):
it remembers nothing about the merchant unless `create_rule` is set ("always categorize this
merchant this way"). Optionally the same answer is applied, as further one-time edits, to the
merchant's other pending rows; the response carries an opaque undo token (every row and the rule
the accept touched, as it was before) that `POST /review/undo` replays. The token is held by the client, not stored: nothing about it is trusted beyond ids the
caller could already patch through `PATCH /transactions/{id}`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from spend_analyzer.api import schemas
from spend_analyzer.api.deps import SessionDep
from spend_analyzer.api.routers.transactions import _transaction_response
from spend_analyzer.api.services import crud
from spend_analyzer.classify import cascade
from spend_analyzer.core.errors import ConfigError

router = APIRouter(tags=["review"])


@router.post("/review/accept", response_model=schemas.ReviewAcceptResponse)
def accept_review(
    body: schemas.ReviewAcceptRequest, session: SessionDep
) -> schemas.ReviewAcceptResponse:
    txn = crud.get_transaction(session, body.transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    try:
        result = cascade.accept_review(
            session,
            body.transaction_id,
            category_key=body.category_key,
            subcategory_key=body.subcategory_key,
            create_rule=body.create_rule,
            similar_ids=body.similar_ids,
        )
    except ConfigError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    session.commit()
    session.refresh(txn)
    return schemas.ReviewAcceptResponse(
        transaction=_transaction_response(session, txn),
        similar_ids=list(result.similar_ids),
        undo=schemas.AcceptUndo.model_validate(result.undo, from_attributes=True),
    )


@router.post("/review/undo", response_model=schemas.ReviewUndoResponse)
def undo_review(body: schemas.AcceptUndo, session: SessionDep) -> schemas.ReviewUndoResponse:
    """Reverse one accept from its undo token; `restored` is how many transactions went back."""
    undo = cascade.AcceptUndo(
        transactions=tuple(
            cascade.TransactionSnapshot(**t.model_dump()) for t in body.transactions
        ),
        rule=cascade.RuleSnapshot(**body.rule.model_dump()) if body.rule is not None else None,
    )
    restored = cascade.undo_accept(session, undo)
    session.commit()
    return schemas.ReviewUndoResponse(restored=restored)
