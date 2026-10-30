"""`/api/review/*`: the Review queue's accept and undo.

Accepting is a one-time edit of that transaction (`classify.cascade.apply_user_correction`):
it remembers nothing about the merchant unless `create_rule` is set ("always categorize this
merchant this way"). Optionally the same answer is applied, as further one-time edits, to the
merchant's other pending rows; the response carries an opaque undo token (every row and the rule
the accept touched, as it was before and as the accept left it) that `POST /review/undo` replays.
The token is held by the client, not stored, so it is validated on use (known kinds, provenance
and taxonomy ids) and undo only reverts what still equals the state the accept left: a row edited
afterwards, or a rule edited afterwards, is skipped and reported, never overwritten.
`POST /rules/{id}/apply` hands out the same kind of token, so it undoes through the same call.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SessionDep
from personal_finance_analyzer.api.routers.transactions import _transaction_response
from personal_finance_analyzer.api.services import crud
from personal_finance_analyzer.classify import cascade
from personal_finance_analyzer.core.errors import ConfigError

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
            kind=body.kind.value if body.kind is not None else None,
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


def undo_token_from_schema(body: schemas.AcceptUndo) -> cascade.AcceptUndo:
    """The cascade's `AcceptUndo` for a client-supplied token (field values were already checked
    by the schema's enums; `cascade.undo_accept` checks the taxonomy ids and rule ownership)."""

    def state(value: schemas.TransactionState) -> cascade.TransactionState:
        return cascade.TransactionState(**value.model_dump(mode="json"))

    def rule_state(value: schemas.RuleState) -> cascade.RuleState:
        return cascade.RuleState(**value.model_dump(mode="json"))

    rule = body.rule
    return cascade.AcceptUndo(
        transactions=tuple(
            cascade.TransactionSnapshot(
                t.transaction_id, state(t.before), state(t.after), t.dedupe_hash
            )
            for t in body.transactions
        ),
        rule=cascade.RuleSnapshot(
            rule.rule_id,
            rule_state(rule.before) if rule.before is not None else None,
            rule_state(rule.after),
        )
        if rule is not None
        else None,
    )


@router.post("/review/undo", response_model=schemas.ReviewUndoResponse)
def undo_review(body: schemas.AcceptUndo, session: SessionDep) -> schemas.ReviewUndoResponse:
    """Reverse one accept (or one rule application) from its undo token. `restored` is how many
    transactions went back; `skipped` lists the ones left alone because they changed afterwards,
    and `rule_skipped` says the accept's rule was left alone for the same reason."""
    try:
        result = cascade.undo_accept(session, undo_token_from_schema(body))
    except ConfigError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    session.commit()
    return schemas.ReviewUndoResponse(
        restored=len(result.restored),
        skipped=list(result.skipped),
        rule_skipped=result.rule_skipped,
    )
