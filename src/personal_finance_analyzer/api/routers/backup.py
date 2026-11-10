"""`/api/backup/export` and `/api/backup/restore`: move all data to another machine.

Both are `POST`: the export carries a passphrase (never in a URL, which ends up in logs and
history), and a state-changing route must not be reachable by a plain link or `<img>`. Like every
`/api` route they sit behind the Host allowlist and the per-launch token (`api/security.py`).

`export` builds the archive in a private scratch directory under the data directory (not the
system temp directory) and streams it back, removing it afterwards; the keychain API key is never
part of it.

`restore` takes the archive as the raw request body (`application/octet-stream`, not multipart:
a multipart upload is spooled by the framework into the system temp directory, which is often
RAM-backed and is not protected like the data directory, and is then copied a second time). The
body is streamed straight into a private 0700 work directory next to the data directory, cut off
with 413 the moment it passes `MAX_BACKUP_UPLOAD_BYTES`, and deleted on every exit path. The
passphrase, which a raw body cannot carry, travels in the `X-Backup-Passphrase` header as
base64 of its UTF-8 bytes (headers are latin-1; base64 keeps any passphrase intact). The archive is
then verified in a staging directory, this server is quiesced (`api/restore_gate.py`), the data
directory is swapped and the database reopened. The previous data directory is kept next to it,
never deleted.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from personal_finance_analyzer.api.restore_gate import RestoreGate
from personal_finance_analyzer.api.services.uploads import (
    MAX_BACKUP_UPLOAD_BYTES,
    stream_body_to_file,
)
from personal_finance_analyzer.backup import archive
from personal_finance_analyzer.backup.errors import BackupBusyError, BackupError, RestoreTargetError
from personal_finance_analyzer.core.paths import db_path, get_home
from personal_finance_analyzer.db.models import Job

router = APIRouter(tags=["backup"])

#: Header carrying the restore passphrase: base64 of its UTF-8 bytes.
PASSPHRASE_HEADER = "X-Backup-Passphrase"
#: Set on an export that left files out (a name that cannot be stored portably, or larger than a
#: restore accepts): URL-quoted JSON `{"count": n, "examples": [first few names]}`.
SKIPPED_HEADER = "X-Backup-Skipped"
UPLOAD_CONTENT_TYPE = "application/octet-stream"


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
    #: PDFs written into the documents folder / already there with the same content, and other
    #: files in that folder that are not part of the backup (left untouched).
    documents_restored: int = 0
    documents_already_present: int = 0
    documents_unrelated: int = 0
    #: Set when some PDFs could not be moved into place (see the CLI message); empty otherwise.
    documents_pending: str | None = None


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
    tmp_dir = archive.make_work_dir(get_home(), "export")
    out = tmp_dir / archive.default_filename()
    try:
        manifest = archive.create_backup(out, passphrase=passphrase)
    except BackupError as exc:
        archive.remove_work_dir(tmp_dir)
        raise _http_error(exc) from exc
    except BaseException:
        archive.remove_work_dir(tmp_dir)
        raise
    headers = {"Cache-Control": "no-store"}
    left_out = [*manifest.skipped, *manifest.oversized]
    if left_out:  # files that are NOT in the download: the page warns about them
        headers[SKIPPED_HEADER] = quote(
            json.dumps({"count": len(left_out), "examples": [n[:120] for n in left_out[:5]]})
        )
    return FileResponse(
        out,
        media_type="application/octet-stream",
        filename=out.name,
        headers=headers,
        background=BackgroundTask(archive.remove_work_dir, tmp_dir),
    )


def _require_own_data_directory(request: Request) -> None:
    engine = request.app.state.engine
    if engine.url.database is None or Path(engine.url.database).resolve() != db_path().resolve():
        raise HTTPException(
            status_code=409, detail="this server is not using the data directory to restore into"
        )


def _decode_passphrase(header: str | None) -> str | None:
    if not header:
        return None
    try:
        return base64.b64decode(header, validate=True).decode("utf-8") or None
    except (binascii.Error, UnicodeDecodeError):
        raise HTTPException(
            status_code=400, detail=f"{PASSPHRASE_HEADER} must be base64 of the UTF-8 passphrase"
        ) from None


def _restore(request: Request, upload: Path, passphrase: str | None) -> archive.RestoreResult:
    engine = request.app.state.engine
    gate: RestoreGate = request.app.state.restore_gate
    _require_own_data_directory(request)
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


@router.post(
    "/backup/restore",
    response_model=BackupRestoreResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "The .pfabackup archive, as the raw request body.",
            "content": {UPLOAD_CONTENT_TYPE: {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def restore_backup(
    request: Request,
    passphrase_b64: Annotated[
        str | None,
        Header(
            alias=PASSPHRASE_HEADER,
            description="Base64 of the UTF-8 passphrase; omit for an unencrypted backup.",
        ),
    ] = None,
) -> BackupRestoreResponse:
    """Replace all current data with the uploaded backup (the old data directory is kept beside
    it, see `previous_dir`)."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != UPLOAD_CONTENT_TYPE:
        raise HTTPException(
            status_code=415, detail=f"send the backup as the raw body ({UPLOAD_CONTENT_TYPE})"
        )
    passphrase = _decode_passphrase(passphrase_b64)
    _require_own_data_directory(request)  # before taking gigabytes off the wire
    work = await run_in_threadpool(archive.make_sibling_work_dir, get_home(), "restore-upload")
    try:
        upload = work / "upload.pfabackup"
        await stream_body_to_file(request, upload, max_bytes=MAX_BACKUP_UPLOAD_BYTES)
        try:
            result = await run_in_threadpool(_restore, request, upload, passphrase)
        except BackupError as exc:
            raise _http_error(exc) from exc
    finally:
        archive.remove_work_dir(work)
    return BackupRestoreResponse(
        created_at=result.manifest.created_at,
        counts=result.manifest.counts,
        migrated=result.migrated,
        previous_dir=str(result.previous_dir) if result.previous_dir else None,
        documents_restored=result.documents_restored,
        documents_already_present=result.documents_already_present,
        documents_unrelated=result.documents_unrelated,
        documents_pending=str(result.documents_pending) if result.documents_pending else None,
    )
