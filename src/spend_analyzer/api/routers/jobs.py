"""`/api/jobs/{id}` and `/api/jobs/{id}/events` (§3.12, A1, D6).

The SSE stream polls the `jobs` row at ~500 ms, opening and closing a short-lived `Session` on
each poll rather than holding one transaction open across the stream, and emits a terminal event
before closing once the job reaches `done`/`error`/`cancelled`. It handles a client disconnect
(`request.is_disconnected()`) without leaking the generator.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from spend_analyzer.api import schemas
from spend_analyzer.api.deps import SessionDep, SessionFactoryDep
from spend_analyzer.db.models import Job

router = APIRouter(tags=["jobs"])

#: How often the SSE stream re-reads the `jobs` row.
_POLL_INTERVAL_S = 0.5

_TERMINAL_STATUSES = frozenset({"done", "error", "cancelled"})


def _result_payload(job: Job) -> dict[str, object]:
    if not job.result_json:
        return {}
    try:
        payload = json.loads(job.result_json)
    except ValueError:  # pragma: no cover - defensive
        return {}
    return payload if isinstance(payload, dict) else {}


def _job_response(job: Job, session: Session | None = None) -> schemas.Job:
    """Build the response for `job`. With `session`, an `import` job that chained a `classify`
    job (job_tasks.py) also carries that chained job's own result counts once it is done —
    `session` is optional only because the SSE stream's disconnect-checking loop above already
    has one open per poll and passes it; a caller with no session just gets the chain id."""
    progress = (job.done / job.total) if job.total > 0 else (1.0 if job.status == "done" else 0.0)
    payload = _result_payload(job)
    message = payload.get("message") if isinstance(payload.get("message"), str) else None
    classify_job_id = payload.get("classify_job_id")
    classify_job_id = classify_job_id if isinstance(classify_job_id, str) else None

    classified: int | None = None
    needs_review: int | None = None
    if classify_job_id is not None and session is not None:
        classify_job = session.get(Job, classify_job_id)
        if classify_job is not None:
            classify_payload = _result_payload(classify_job)
            classified_raw = classify_payload.get("classified")
            needs_review_raw = classify_payload.get("needs_review")
            classified = classified_raw if isinstance(classified_raw, int) else None
            needs_review = needs_review_raw if isinstance(needs_review_raw, int) else None

    inserted = payload.get("inserted")
    skipped_duplicates = payload.get("skipped_duplicates")
    restored_edits = payload.get("restored_edits")
    unmatched_edits = payload.get("unmatched_edits")

    return schemas.Job(
        id=job.id,
        kind=job.kind,
        status=job.status,
        progress=min(max(progress, 0.0), 1.0),
        message=message,
        group_id=job.llm_run_group_id,
        error_detail=job.error_detail,
        classify_job_id=classify_job_id,
        classified=classified,
        needs_review=needs_review,
        inserted=inserted if isinstance(inserted, int) else None,
        skipped_duplicates=skipped_duplicates if isinstance(skipped_duplicates, int) else None,
        restored_edits=restored_edits if isinstance(restored_edits, int) else None,
        unmatched_edits=unmatched_edits if isinstance(unmatched_edits, int) else None,
    )


@router.get("/jobs/{job_id}", response_model=schemas.Job)
def get_job(job_id: str, session: SessionDep) -> schemas.Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_response(job, session)


@router.get("/jobs/{job_id}/events")
async def job_events(
    job_id: str,
    request: Request,
    session_factory: SessionFactoryDep,
    token: str | None = None,
) -> StreamingResponse:
    with session_factory() as session:
        if session.get(Job, job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")

    async def event_stream() -> AsyncIterator[str]:
        last_done = -1
        while True:
            if await request.is_disconnected():
                return
            with session_factory() as poll_session:
                job = poll_session.get(Job, job_id)
                if job is None:  # pragma: no cover - defensive; jobs rows are never deleted
                    return
                done, status = job.done, job.status
                if done != last_done or status in _TERMINAL_STATUSES:
                    last_done = done
                    response_json = _job_response(job, poll_session).model_dump_json()
                else:
                    response_json = None
            if response_json is not None:
                yield f"data: {response_json}\n\n"
            if status in _TERMINAL_STATUSES:
                return
            await asyncio.sleep(_POLL_INTERVAL_S)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
