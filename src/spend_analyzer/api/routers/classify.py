"""`/api/classify/*` (§3.12)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import select

from spend_analyzer.api import schemas
from spend_analyzer.api.deps import JobRunnerDep, SessionDep, SessionFactoryDep, SettingsDep
from spend_analyzer.api.services import crud, gateway
from spend_analyzer.core.types import USER_CLASSIFIED
from spend_analyzer.db.models import Transaction
from spend_analyzer.jobs.runner import JobProgress

router = APIRouter(tags=["classify"])


@router.post("/classify/run", response_model=schemas.ClassifyRunResponse, status_code=202)
def run_classify(
    body: schemas.ClassifyRunRequest,
    session: SessionDep,
    settings: SettingsDep,
    runner: JobRunnerDep,
    session_factory: SessionFactoryDep,
) -> schemas.ClassifyRunResponse:
    # A hand-corrected transaction is final: a re-run never reconsiders it.
    stmt = select(Transaction.id).where(Transaction.classified_by.is_distinct_from(USER_CLASSIFIED))
    if body.user_id is not None:
        stmt = stmt.where(Transaction.user_id == body.user_id)
    if body.statement_id is not None:
        stmt = stmt.where(Transaction.statement_id == body.statement_id)
    if body.only_unclassified:
        stmt = stmt.where(crud.unclassified_clause())
    transaction_ids = tuple(session.execute(stmt).scalars().all())

    group_id = str(uuid.uuid4())

    def task(progress: JobProgress) -> dict[str, object]:
        def progress_cb(done: int, total: int, cost_usd: float) -> None:
            progress.update(done, total, cost_usd)

        result = gateway.classify_transactions(
            session_factory,
            transaction_ids,
            cfg=settings.llm,
            group_id=group_id,
            progress_cb=progress_cb,
        )
        return {
            "group_id": result.group_id,
            "classified": result.classified,
            "needs_review": result.needs_review,
            "llm_requests": result.llm_requests,
            "cost_usd": result.cost_usd,
        }

    job_id = runner.run_job(kind="classify", task=task)
    return schemas.ClassifyRunResponse(
        job_id=job_id, group_id=group_id, transaction_count=len(transaction_ids)
    )


@router.get("/classify/preview", response_model=list[schemas.ClassifyPreviewRow])
def preview_classify(
    session: SessionDep,
    user_id: int | None = None,
    statement_id: int | None = None,
) -> list[schemas.ClassifyPreviewRow]:
    """The exact CSV the next run would send (A5), decoded back into rows. No network call."""
    stmt = select(Transaction.id, Transaction.merchant_key, Transaction.description_clean).where(
        crud.unclassified_clause()
    )
    if user_id is not None:
        stmt = stmt.where(Transaction.user_id == user_id)
    if statement_id is not None:
        stmt = stmt.where(Transaction.statement_id == statement_id)
    rows = session.execute(stmt).all()
    transaction_ids = [row[0] for row in rows]
    if not transaction_ids:
        return []
    gateway.preview_egress(session, transaction_ids)  # validates the payload is egress-safe
    return [
        schemas.ClassifyPreviewRow(merchant_key=row[1], description_clean=row[2]) for row in rows
    ]


@router.get("/classify/runs", response_model=list[schemas.LlmRun])
def list_classify_runs(session: SessionDep) -> list[schemas.LlmRun]:
    runs = crud.list_llm_runs(session)
    return [
        schemas.LlmRun(
            id=run.id,
            group_id=run.group_id,
            provider=run.provider,
            model=run.model,
            schema_mode=run.schema_mode,
            cost_usd=run.cost_usd,
            prompt_tokens=run.tokens_in,
            completion_tokens=run.tokens_out,
            created_at=run.started_at,
        )
        for run in runs
    ]
