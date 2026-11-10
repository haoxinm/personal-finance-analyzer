"""Job bodies shared by the statements and classify routers.

**Job flow (P2-C plan section):** `POST /api/statements/{id}/extract` enqueues an `import` job
that calls `confirm_import`, then enqueues a `classify` job for the returned `transaction_ids`.
Both tasks get their own `Session` from the runner's `session_factory` — never the request's
session, since the request has already returned by the time these run.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from personal_finance_analyzer.api.services import gateway
from personal_finance_analyzer.config import LLMConfig
from personal_finance_analyzer.jobs.runner import JobProgress, JobRunner


def enqueue_import_then_classify(
    runner: JobRunner,
    session_factory: sessionmaker[Session],
    *,
    statement_id: int,
    issuer_id: int,
    parser_id: str | None,
    layout_spec_id: int | None,
    remember: bool,
    cfg: LLMConfig,
    pages: Sequence[int] | None = None,
) -> str:
    """Enqueue the import job for `statement_id`; on success, enqueue a classify job for the
    transactions it inserted. Returns the import job's id immediately."""

    def import_task(progress: JobProgress) -> dict[str, Any]:
        with session_factory() as session:
            result = gateway.confirm_import(
                session,
                statement_id,
                issuer_id=issuer_id,
                parser_id=parser_id,
                layout_spec_id=layout_spec_id,
                remember=remember,
                pages=pages,
            )
            session.commit()
        progress.update(1, 1)
        classify_job_id: str | None = None
        if result.transaction_ids:
            group_id = str(uuid.uuid4())
            classify_job_id = runner.run_job(
                kind="classify",
                task=_classify_task(
                    session_factory,
                    transaction_ids=result.transaction_ids,
                    cfg=cfg,
                    group_id=group_id,
                ),
            )
        return {
            "inserted": result.inserted,
            "skipped_duplicates": result.skipped_duplicates,
            "transaction_ids": list(result.transaction_ids),
            "layout_drift": result.layout_drift,
            "warnings": list(result.warnings),
            # Surfaced by `GET /api/jobs/{id}` (jobs.py's `_job_response`) as `classify_job_id`,
            # so a caller can follow the chain and report an exact "N need review" once it's
            # done, without this module needing a `parent_job_id` column (db/** is frozen here).
            "classify_job_id": classify_job_id,
        }

    return runner.run_job(kind="import", task=import_task)


def _classify_task(
    session_factory: sessionmaker[Session],
    *,
    transaction_ids: tuple[int, ...],
    cfg: LLMConfig,
    group_id: str,
) -> Any:
    def task(progress: JobProgress) -> dict[str, Any]:
        def progress_cb(done: int, total: int, cost_usd: float) -> None:
            progress.update(done, total, cost_usd)

        result = gateway.classify_transactions(
            session_factory,
            transaction_ids,
            cfg=cfg,
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

    return task
