"""`/api/issuers` (§3.12, A24, D14). `name` and `match_terms` are local-only (I1b) — never
egressed; this router never imports anything from `classify/llm/`."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SessionDep
from personal_finance_analyzer.api.services import crud, issuer_preview
from personal_finance_analyzer.db.models import Issuer as IssuerModel

router = APIRouter(tags=["issuers"])


_CONFLICT_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {
        "model": schemas.IssuerConflictResponse,
        "description": "An issuer with this name (or slug) already exists.",
    }
}


def _conflict(exc: crud.DuplicateIssuerError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"message": str(exc), "existing_issuer_id": exc.existing.id},
    )


def _issuer_schema(issuer: IssuerModel) -> schemas.Issuer:
    return schemas.Issuer(
        id=issuer.id,
        name=issuer.name,
        slug=issuer.slug,
        match_terms=json.loads(issuer.match_terms),
        default_spec_id=issuer.default_spec_id,
    )


@router.get("/issuers", response_model=list[schemas.Issuer])
def list_issuers(session: SessionDep) -> list[schemas.Issuer]:
    return [_issuer_schema(i) for i in crud.list_issuers(session)]


@router.post(
    "/issuers", response_model=schemas.Issuer, status_code=201, responses=_CONFLICT_RESPONSES
)
def create_issuer(body: schemas.IssuerCreate, session: SessionDep) -> schemas.Issuer:
    try:
        issuer = crud.create_issuer(session, name=body.name, match_terms=body.match_terms)
    except crud.DuplicateIssuerError as exc:
        raise _conflict(exc) from None
    session.commit()
    return _issuer_schema(issuer)


@router.patch("/issuers/{issuer_id}", response_model=schemas.Issuer, responses=_CONFLICT_RESPONSES)
def update_issuer(
    issuer_id: int, body: schemas.IssuerUpdate, session: SessionDep
) -> schemas.Issuer:
    issuer = crud.get_issuer(session, issuer_id)
    if issuer is None:
        raise HTTPException(status_code=404, detail="issuer not found")
    try:
        issuer = crud.update_issuer(
            session,
            issuer,
            name=body.name,
            match_terms=body.match_terms,
            default_spec_id=body.default_spec_id,
        )
    except crud.DuplicateIssuerError as exc:
        session.rollback()
        raise _conflict(exc) from None
    session.commit()
    return _issuer_schema(issuer)


@router.delete("/issuers/{issuer_id}", status_code=204)
def delete_issuer(issuer_id: int, session: SessionDep) -> None:
    issuer = crud.get_issuer(session, issuer_id)
    if issuer is None:
        raise HTTPException(status_code=404, detail="issuer not found")
    crud.delete_issuer(session, issuer)
    session.commit()


@router.post("/issuers/preview-match", response_model=list[schemas.IssuerMatchPreviewRow])
def preview_issuer_match(
    body: schemas.IssuerMatchPreviewRequest, session: SessionDep
) -> list[schemas.IssuerMatchPreviewRow]:
    """A dry run for A24: which existing statements `body.match_terms` would match, before
    saving them to a real issuer (never writes anything; never returns page text)."""
    rows = issuer_preview.preview_match(session, body.match_terms)
    return [
        schemas.IssuerMatchPreviewRow(
            statement_id=row.statement_id,
            original_name=row.original_name,
            status=row.status,
            period_start=row.period_start,
            period_end=row.period_end,
        )
        for row in rows
    ]
