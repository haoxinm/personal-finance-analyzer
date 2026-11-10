"""`/api/watch/*`: folder imports (`watch/`): status, "scan now", what was imported, what needs
attention, and Retry.

Switching watching on and the interval are ordinary settings (`/api/settings`, ``[watch]``). A scan
runs in the background; `POST /watch/scan` returns at once and the page polls `GET /watch/status`.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import func, select

from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SessionDep
from personal_finance_analyzer.db.models import WatchedFile
from personal_finance_analyzer.watch import scanner
from personal_finance_analyzer.watch.service import WatchService, WatchStatus

router = APIRouter(tags=["folder-imports"])


def _service(request: Request) -> WatchService:
    service: WatchService = request.app.state.watch
    return service


def _status(status: WatchStatus) -> schemas.WatchStatus:
    last = status.last_scan
    return schemas.WatchStatus(
        enabled=status.enabled,
        interval_seconds=status.interval_seconds,
        documents_dir=status.documents_dir,
        running=status.running,
        next_scan_at=status.next_scan_at,
        last_scan=(
            schemas.WatchScanSummary(
                started_at=last.started_at,
                finished_at=last.finished_at,
                scanned=last.scanned,
                imported=last.imported,
                known=last.known,
                failed=last.failed,
                ignored=last.ignored,
                waiting=last.waiting,
                busy=last.busy,
                error=last.error,
            )
            if last is not None
            else None
        ),
        problem=status.problem,
    )


def _warnings(record: WatchedFile) -> list[str]:
    if not record.warnings:
        return []
    try:
        value = json.loads(record.warnings)
    except ValueError:
        return []
    return [str(w) for w in value] if isinstance(value, list) else []


def _file(record: WatchedFile) -> schemas.WatchFile:
    kind: Literal["statement", "tax"] | None = (
        "statement" if record.kind == "statement" else "tax" if record.kind == "tax" else None
    )
    return schemas.WatchFile(
        id=record.id,
        path=record.path,
        outcome=record.outcome,
        code=record.code,
        message=record.message,
        hint=record.hint,
        kind=kind,
        folder_person=record.folder_person,
        user_id=record.user_id,
        statement_id=record.statement_id,
        tax_document_id=record.tax_document_id,
        warnings=_warnings(record),
        first_seen=record.first_seen,
        last_seen=record.last_seen,
    )


@router.get("/watch/status", response_model=schemas.WatchStatus)
def get_status(request: Request) -> schemas.WatchStatus:
    return _status(_service(request).status())


@router.post("/watch/scan", response_model=schemas.WatchScanStarted, status_code=202)
def scan_now(request: Request) -> schemas.WatchScanStarted:
    """Scan the documents folder now (also when watching is off: this is the explicit request).
    Returns at once; ``started`` is false when a scan was already running."""
    service = _service(request)
    started = service.scan_now()
    return schemas.WatchScanStarted(started=started, status=_status(service.status()))


@router.get("/watch/files", response_model=schemas.WatchFileList)
def list_files(
    session: SessionDep,
    view: Annotated[Literal["recent", "problems"], Query()] = "recent",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> schemas.WatchFileList:
    """``recent``: the newest imports (and files that were already in the app). ``problems``:
    files that need attention (failed, or not in a folder the app imports from)."""
    outcomes = ("imported", "known") if view == "recent" else ("failed", "ignored")
    where = WatchedFile.outcome.in_(outcomes)
    total = session.scalar(select(func.count()).select_from(WatchedFile).where(where)) or 0
    rows = session.scalars(
        select(WatchedFile)
        .where(where)
        .order_by(WatchedFile.last_seen.desc(), WatchedFile.id.desc())
        .limit(limit)
    ).all()
    return schemas.WatchFileList(items=[_file(r) for r in rows], total=total)


@router.post("/watch/files/{file_id}/retry", status_code=204, response_class=Response)
def retry_file(file_id: int, session: SessionDep) -> Response:
    """Forget a problem so the next scan looks at the file again (after the cause is fixed)."""
    if not scanner.retry(session, file_id):
        raise HTTPException(status_code=404, detail="no such problem to retry")
    return Response(status_code=204)
