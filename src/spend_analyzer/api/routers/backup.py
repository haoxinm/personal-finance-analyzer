"""`/api/backup/export` and `/api/backup/restore`: move all data to another machine.

Both are `POST`: the export carries a passphrase (never in a URL, which ends up in logs and
history), and a state-changing route must not be reachable by a plain link or `<img>`. Like every
`/api` route they sit behind the Host allowlist and the per-launch token (`api/security.py`).

`export` builds the archive in a private temporary directory and streams it back; the keychain
API key is never part of it. `restore` streams the upload to disk (capped at
`MAX_BACKUP_UPLOAD_BYTES`), verifies it in a staging directory, then quiesces this server
(`api/restore_gate.py`), swaps the data directory and reopens the database. The previous data
directory is kept next to it, never deleted.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from spend_analyzer.api.restore_gate import RestoreGate
from spend_analyzer.api.services.uploads import MAX_BACKUP_UPLOAD_BYTES, stream_upload_to_file
from spend_analyzer.backup import archive
from spend_analyzer.backup.errors import BackupBusyError, BackupError, RestoreTargetError
from spend_analyzer.core.paths import db_path
from spend_analyzer.db.models import Job

router = APIRouter(tags=["backup"])


class BackupExportRequest(BaseModel):
    """`passphrase` encrypts the archive (at least 8 characters). Sending no passphrase is only
    accepted with an explicit `encrypt: false`."""

    model_config = ConfigDict(extra="forbid")

    passphrase: str | None = None
    encrypt: bool = True


class BackupRestoreResponse(BaseModel):
    created_at: str
    counts: dict[str, int]
    migrated: bool
    previous_dir: str | None


def _http_error(exc: BackupError) -> HTTPException:
    conflict = isinstance(exc, (RestoreTargetError, BackupBusyError))
    return HTTPException(status_code=409 if conflict else 400, detail=str(exc))


@router.post("/backup/export")
def export_backup(body: BackupExportRequest) -> FileResponse:
    """Build a backup of the current data and return it as a download."""
    if body.encrypt and not body.passphrase:
        raise HTTPException(
            status_code=400,
            detail="a passphrase is required (or send encrypt=false to skip encryption)",
        )
    passphrase = body.passphrase if body.encrypt else None
    tmp_dir = Path(tempfile.mkdtemp(prefix="spend-export-"))
    out = tmp_dir / archive.default_filename()
    try:
        archive.create_backup(out, passphrase=passphrase)
    except BackupError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise _http_error(exc) from exc
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return FileResponse(
        out,
        media_type="application/octet-stream",
        filename=out.name,
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True),
    )


def _restore(request: Request, upload: Path, passphrase: str | None) -> archive.RestoreResult:
    engine = request.app.state.engine
    gate: RestoreGate = request.app.state.restore_gate
    if engine.url.database is None or Path(engine.url.database).resolve() != db_path().resolve():
        raise HTTPException(
            status_code=409, detail="this server is not using the data directory to restore into"
        )
    quiesced = False

    def quiesce() -> None:
        nonlocal quiesced
        gate.begin_exclusive()
        quiesced = True
        with Session(engine) as session:
            active = session.scalar(
                select(func.count()).select_from(Job).where(Job.status.in_(("queued", "running")))
            )
        if active:
            raise BackupBusyError("a background job is still running; wait for it to finish")
        engine.dispose()  # close pooled connections so the swapped-in file is what reopens

    try:
        return archive.restore_backup(
            upload, passphrase=passphrase, replace=True, before_swap=quiesce
        )
    finally:
        if quiesced:
            engine.dispose()
            gate.end_exclusive()


@router.post("/backup/restore", response_model=BackupRestoreResponse)
async def restore_backup(
    request: Request,
    file: Annotated[UploadFile, File()],
    passphrase: Annotated[str | None, Form()] = None,
) -> BackupRestoreResponse:
    """Replace all current data with the uploaded backup (the old data directory is kept beside
    it, see `previous_dir`)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="spend-restore-"))
    try:
        upload = tmp_dir / "upload.spendbackup"
        await stream_upload_to_file(file, upload, max_bytes=MAX_BACKUP_UPLOAD_BYTES)
        try:
            result = await run_in_threadpool(_restore, request, upload, passphrase or None)
        except BackupError as exc:
            raise _http_error(exc) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return BackupRestoreResponse(
        created_at=result.manifest.created_at,
        counts=result.manifest.counts,
        migrated=result.migrated,
        previous_dir=str(result.previous_dir) if result.previous_dir else None,
    )
