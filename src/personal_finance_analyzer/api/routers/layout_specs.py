"""`/api/layout-specs/*` (§3.12, A19, A21). Specs are immutable `(name, version)` rows; a revise
inserts `version + 1` and never mutates the row it revises.

Every spec this router persists is validated first through `ingest.layout_spec.load_spec` — the
single entry point for every spec regardless of origin (P1-H) — so a spec entered through the
Layout mapper gets exactly the same validation a pasted or LLM-proposed one gets; an invalid spec
never reaches the database.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.responses import PlainTextResponse

from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SessionDep, SettingsDep
from personal_finance_analyzer.api.services import crud
from personal_finance_analyzer.core.errors import ParserError, UnsupportedLayoutError
from personal_finance_analyzer.db.models import LayoutSpec as LayoutSpecModel
from personal_finance_analyzer.db.pii_terms import user_pii_terms
from personal_finance_analyzer.ingest.extract import extract
from personal_finance_analyzer.ingest.layout_spec import LayoutSpecError, load_spec
from personal_finance_analyzer.ingest.normalize import normalize

router = APIRouter(tags=["layout-specs"])


def _spec_schema(spec: LayoutSpecModel) -> schemas.LayoutSpec:
    return schemas.LayoutSpec(
        id=spec.id,
        name=spec.name,
        version=spec.version,
        issuer_id=spec.issuer_id,
        approved=spec.approved,
        source="pasted" if spec.source == "pasted" else "hand_mapped",
    )


def _validate_spec_yaml(spec_yaml: str) -> None:
    """Run `spec_yaml` through `load_spec` for its side effect (raising) alone — this router
    persists the same YAML text it validates, not `load_spec`'s parsed result, so every origin
    (paste, LLM proposal, or the Layout mapper) keeps computing its own `parser_id`/`source` the
    way it already did before this validation existed.

    Raises:
        HTTPException: 422, with `load_spec`'s field-level errors, if `spec_yaml` is invalid.
    """
    try:
        load_spec(spec_yaml, source="user_authored")
    except LayoutSpecError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "errors": [{"field": e.field, "message": e.message} for e in exc.errors],
            },
        ) from None


@router.get("/layout-specs", response_model=list[schemas.LayoutSpec])
def list_layout_specs(session: SessionDep) -> list[schemas.LayoutSpec]:
    return [_spec_schema(s) for s in crud.list_layout_specs(session)]


_CONFLICT_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {"model": schemas.ConflictResponse, "description": "That layout spec already exists."}
}


@router.post(
    "/layout-specs",
    response_model=schemas.LayoutSpec,
    status_code=201,
    responses=_CONFLICT_RESPONSES,
)
def create_layout_spec(body: schemas.LayoutSpecCreate, session: SessionDep) -> schemas.LayoutSpec:
    _validate_spec_yaml(body.spec_yaml)
    try:
        spec = crud.create_layout_spec(
            session, name=body.name, spec_yaml=body.spec_yaml, issuer_id=body.issuer_id
        )
    except crud.DuplicateLayoutSpecError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    session.commit()
    return _spec_schema(spec)


@router.post(
    "/layout-specs/{spec_id}/revise",
    response_model=schemas.LayoutSpec,
    status_code=201,
    responses=_CONFLICT_RESPONSES,
)
def revise_layout_spec(
    spec_id: int, body: schemas.LayoutSpecCreate, session: SessionDep
) -> schemas.LayoutSpec:
    previous = crud.get_layout_spec(session, spec_id)
    if previous is None:
        raise HTTPException(status_code=404, detail="layout spec not found")
    _validate_spec_yaml(body.spec_yaml)
    try:
        spec = crud.revise_layout_spec(
            session, previous, spec_yaml=body.spec_yaml, issuer_id=body.issuer_id
        )
    except crud.DuplicateLayoutSpecError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    session.commit()
    return _spec_schema(spec)


#: Dry-run sample rows are capped, not paginated: the point is a quick sanity check, not a full
#: preview (which the real import/report would give once the spec is actually confirmed).
_DRY_RUN_SAMPLE_LIMIT = 20


@router.post("/layout-specs/dry-run", response_model=schemas.LayoutSpecDryRunResponse)
def dry_run_layout_spec(
    body: schemas.LayoutSpecDryRunRequest, session: SessionDep, settings: SettingsDep
) -> schemas.LayoutSpecDryRunResponse:
    """Parse `body.statement_id`'s staged PDF with `body.spec_yaml`, without writing anything to
    the database (§2c's own "try before you save" workflow) — the Layout mapper's own preview.
    """
    statement = crud.get_statement(session, body.statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="statement not found")

    pdf_path = crud.staged_pdf_path(statement)
    if pdf_path is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "no PDF copy is available for this statement "
                "([privacy] store_pdf_copies was off and it is already confirmed)"
            ),
        )

    try:
        loaded = load_spec(body.spec_yaml, source="user_authored")
    except LayoutSpecError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "errors": [{"field": e.field, "message": e.message} for e in exc.errors],
            },
        ) from None

    doc = extract(pdf_path)
    try:
        parsed = loaded.parser.parse(doc)
    except (UnsupportedLayoutError, ParserError) as exc:
        return schemas.LayoutSpecDryRunResponse(
            txn_count=0,
            total_minor=0,
            currency=settings.ingest.default_currency,
            sample_rows=[],
            warnings=[str(exc)],
            reconciliation_delta_minor=None,
        )

    total_minor = sum(t.amount_minor for t in parsed.transactions)
    currency = (
        parsed.transactions[0].currency
        if parsed.transactions
        else (parsed.account_hint.currency or settings.ingest.default_currency)
    )

    reconciliation_delta_minor: int | None = None
    if parsed.opening_balance_minor is not None and parsed.closing_balance_minor is not None:
        account_type = parsed.account_hint.account_type or "credit"
        expected = (
            (parsed.closing_balance_minor - parsed.opening_balance_minor)
            if account_type == "credit"
            else (parsed.opening_balance_minor - parsed.closing_balance_minor)
        )
        reconciliation_delta_minor = total_minor - expected

    pii_terms = user_pii_terms(session, settings, statement.user_id)
    sample_rows = [
        schemas.LayoutSpecDryRunRow(
            description_clean=normalize(t.description, pii_terms).description_clean,
            posted_date=t.posted_date,
            amount_minor=t.amount_minor,
        )
        for t in parsed.transactions[:_DRY_RUN_SAMPLE_LIMIT]
    ]

    return schemas.LayoutSpecDryRunResponse(
        txn_count=len(parsed.transactions),
        total_minor=total_minor,
        currency=currency,
        sample_rows=sample_rows,
        warnings=list(parsed.warnings),
        reconciliation_delta_minor=reconciliation_delta_minor,
    )


@router.post("/layout-specs/{spec_id}/approve", response_model=schemas.LayoutSpec)
def approve_layout_spec(spec_id: int, session: SessionDep) -> schemas.LayoutSpec:
    spec = crud.get_layout_spec(session, spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="layout spec not found")
    spec = crud.approve_layout_spec(session, spec)
    session.commit()
    return _spec_schema(spec)


@router.get("/layout-specs/{spec_id}/export")
def export_layout_spec(spec_id: int, session: SessionDep) -> PlainTextResponse:
    spec = crud.get_layout_spec(session, spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="layout spec not found")
    return PlainTextResponse(spec.spec_yaml, media_type="application/x-yaml")
