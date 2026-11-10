"""`/api/taxonomy/*` (§3.12, A14)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SessionDep
from personal_finance_analyzer.api.services import crud, gateway
from personal_finance_analyzer.db.models import Subcategory as SubcategoryModel

router = APIRouter(tags=["taxonomy"])


def _subcategory_schema(sub: SubcategoryModel) -> schemas.Subcategory:
    return schemas.Subcategory(
        id=sub.id,
        key=sub.key,
        name=sub.label,
        pending=sub.status == "pending_approval",
        merged_into=str(sub.merged_into) if sub.merged_into is not None else None,
    )


@router.get("/taxonomy", response_model=list[schemas.Category])
def get_taxonomy(session: SessionDep) -> list[schemas.Category]:
    categories = crud.list_categories_with_subcategories(session)
    return [
        schemas.Category(
            key=cat.key,
            name=cat.label,
            subcategories=[
                _subcategory_schema(sub) for sub in crud.subcategories_for_category(session, cat.id)
            ],
        )
        for cat in categories
    ]


@router.post("/taxonomy/subcategories/{subcategory_id}/approve", response_model=schemas.Subcategory)
def approve_subcategory(subcategory_id: int, session: SessionDep) -> schemas.Subcategory:
    sub = crud.get_subcategory(session, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="subcategory not found")
    gateway.approve_subcategory(session, subcategory_id)
    session.commit()
    session.refresh(sub)
    return _subcategory_schema(sub)


@router.post("/taxonomy/subcategories/{subcategory_id}/merge", response_model=schemas.Subcategory)
def merge_subcategory(
    subcategory_id: int, body: schemas.MergeRequest, session: SessionDep
) -> schemas.Subcategory:
    sub = crud.get_subcategory(session, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="subcategory not found")
    gateway.merge_subcategory(session, subcategory_id, into_id=body.into_id)
    session.commit()
    session.refresh(sub)
    return _subcategory_schema(sub)
