"""`/api/rules` (§3.12)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session

from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SessionDep
from personal_finance_analyzer.api.services import crud
from personal_finance_analyzer.classify import cascade
from personal_finance_analyzer.db.models import Category, Subcategory
from personal_finance_analyzer.db.models import Rule as RuleModel

router = APIRouter(tags=["rules"])


def _rule_schema(
    session: Session, rule: RuleModel, *, existing_matches: int | None = None
) -> schemas.Rule:
    category_row = session.get(Category, rule.category_id)
    subcategory_row = session.get(Subcategory, rule.subcategory_id)
    return schemas.Rule(
        id=rule.id,
        pattern=rule.pattern,
        category_key=category_row.key if category_row is not None else "",
        subcategory_key=subcategory_row.key if subcategory_row is not None else "",
        kind=rule.kind_override,
        source=rule.source,
        match_type=rule.match_type,
        priority=rule.priority,
        existing_matches=existing_matches,
    )


@router.get("/rules", response_model=list[schemas.Rule])
def list_rules(session: SessionDep) -> list[schemas.Rule]:
    return [_rule_schema(session, r) for r in crud.list_rules(session)]


@router.post("/rules", response_model=schemas.Rule, status_code=201)
def create_rule(body: schemas.RuleCreate, session: SessionDep) -> schemas.Rule:
    category = crud.category_by_key(session, body.category_key)
    if category is None:
        raise HTTPException(status_code=400, detail=f"unknown category {body.category_key!r}")
    subcategory = crud.subcategory_by_key(session, category.id, body.subcategory_key)
    if subcategory is None:
        raise HTTPException(status_code=400, detail=f"unknown subcategory {body.subcategory_key!r}")
    rule = crud.create_rule(
        session,
        pattern=body.pattern,
        match_type=body.match_type,
        category_id=category.id,
        subcategory_id=subcategory.id,
        kind_override=body.kind,
    )
    existing_matches = len(cascade.rule_target_ids(session, rule))
    session.commit()
    return _rule_schema(session, rule, existing_matches=existing_matches)


def _user_rule_or_error(session: Session, rule_id: int) -> RuleModel:
    rule = crud.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    if rule.source != "user":
        raise HTTPException(status_code=400, detail="only your own rules can be applied")
    return rule


@router.get("/rules/{rule_id}/apply-preview", response_model=schemas.RuleApplyPreview)
def preview_apply_rule(rule_id: int, session: SessionDep) -> schemas.RuleApplyPreview:
    """What applying a user rule to existing transactions would change right now (nothing is
    changed): how many rows, how many users own them, how many would get a different kind. A rule
    with no owner applies across every user's transactions."""
    preview = cascade.preview_rule_application(session, _user_rule_or_error(session, rule_id))
    return schemas.RuleApplyPreview(
        matches=preview.matches,
        user_count=preview.user_count,
        kind_changes=preview.kind_changes,
        all_users=preview.all_users,
    )


@router.post("/rules/{rule_id}/apply", response_model=schemas.RuleApplyResponse)
def apply_rule(rule_id: int, session: SessionDep) -> schemas.RuleApplyResponse:
    """Apply a user rule to the transactions that already exist (rules otherwise only affect
    future classification). Never overwrites a hand-corrected transaction or one a
    higher-priority rule claims; `updated` is how many rows changed. `undo` is a token for
    `POST /review/undo` that puts those rows back (rows edited since are skipped)."""
    rule = _user_rule_or_error(session, rule_id)
    updated, undo = cascade.apply_rule_to_existing(session, rule)
    session.commit()
    return schemas.RuleApplyResponse(
        updated=updated, undo=schemas.AcceptUndo.model_validate(undo, from_attributes=True)
    )


@router.patch("/rules/{rule_id}", response_model=schemas.Rule)
def update_rule(rule_id: int, body: schemas.RuleUpdate, session: SessionDep) -> schemas.Rule:
    rule = crud.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")

    category_id = None
    if body.category_key is not None:
        category = crud.category_by_key(session, body.category_key)
        if category is None:
            raise HTTPException(status_code=400, detail=f"unknown category {body.category_key!r}")
        category_id = category.id

    subcategory_id = None
    if body.subcategory_key is not None:
        target_category_id = category_id if category_id is not None else rule.category_id
        subcategory = crud.subcategory_by_key(session, target_category_id, body.subcategory_key)
        if subcategory is None:
            raise HTTPException(
                status_code=400, detail=f"unknown subcategory {body.subcategory_key!r}"
            )
        subcategory_id = subcategory.id

    rule = crud.update_rule(
        session,
        rule,
        pattern=body.pattern,
        category_id=category_id,
        subcategory_id=subcategory_id,
        kind_override=body.kind,
    )
    session.commit()
    return _rule_schema(session, rule)


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, session: SessionDep) -> None:
    rule = crud.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    crud.delete_rule(session, rule)
    session.commit()
