"""Shared upload hardening for PDF endpoints (A12), used by `/api/statements` and `/api/tax`.

A 25 MB cap enforced while streaming (never buffers an oversized file whole), a content-type
check, a `%PDF-` magic-byte check, a `sha256`-derived stored filename (never the user-supplied
name), and a sanitized `original_name` before it is ever echoed back.

SECURITY-SENSITIVE: changes here affect every upload route. Moved verbatim from
`api/routers/statements.py`; behaviour must stay identical.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from fastapi import HTTPException, Request, UploadFile

from personal_finance_analyzer.core.paths import staging_dir
from personal_finance_analyzer.ingest.extract import sha256_file

#: A12: hard cap on an uploaded file's size.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
#: Cap for a `.pfabackup` upload (`/api/backup/restore`): it bundles every kept PDF, so it is far
#: larger than one statement, but still bounded (and streamed straight into a private work
#: directory as the request body, never held in memory or spooled to the system temp directory).
MAX_BACKUP_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
PDF_MAGIC = b"%PDF-"
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]")


def sanitize_original_name(name: str, default: str = "statement.pdf") -> str:
    """Strip path components and anything but a conservative character set (A12: no path
    traversal, and never trust a client-supplied filename for display without cleaning it)."""
    base = Path(name).name or default
    return _UNSAFE_NAME_CHARS.sub("_", base)[:255]


async def read_pdf_upload(file: UploadFile) -> bytes:
    """Validate and read an uploaded PDF.

    Raises:
        HTTPException: 400 for a non-PDF content type, a file over the 25 MB cap (checked while
            streaming), or content that does not start with the `%PDF-` magic bytes.
    """
    if file.content_type not in (None, "application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=400, detail="only application/pdf uploads are accepted")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="file exceeds the 25 MB upload limit")
        chunks.append(chunk)
    data = b"".join(chunks)

    if not data.startswith(PDF_MAGIC):
        raise HTTPException(status_code=400, detail="not a PDF file")
    return data


async def stream_body_to_file(request: Request, dest: Path, *, max_bytes: int) -> int:
    """Write the request body to ``dest`` as it arrives, enforcing ``max_bytes`` while streaming,
    and return its size.

    The body is never spooled anywhere else first (a multipart ``UploadFile`` would be copied to
    the system temp directory, which may be RAM-backed and is not protected like the data
    directory) and an oversized one is cut off at the cap, not read to the end.

    Raises:
        HTTPException: 413 when the announced ``Content-Length`` or the bytes actually received
            exceed ``max_bytes`` (``dest`` is left partial; the caller discards its directory).
    """
    too_big = HTTPException(
        status_code=413, detail=f"upload exceeds the {max_bytes // (1024 * 1024)} MB cap"
    )
    announced = request.headers.get("content-length")
    if announced is not None and announced.isdigit() and int(announced) > max_bytes:
        raise too_big
    total = 0
    with dest.open("wb") as out:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                raise too_big
            out.write(chunk)
    return total


def save_upload(file_bytes: bytes) -> Path:
    """Write `file_bytes` to `<staging_dir>/<sha256>.pdf` (A12) and return the path. Byte-
    identical re-uploads simply overwrite the same path (I9's no-op is enforced downstream, by
    `propose_import` matching on `file_sha256`, not by skipping the write here)."""
    tmp_dir = staging_dir()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=tmp_dir, delete=False, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)
    digest = sha256_file(tmp_path)
    final_path = tmp_dir / f"{digest}.pdf"
    if final_path.exists():
        # Byte-identical copy already stored (the name is the content hash). Don't replace it:
        # on Windows that fails while another request has the stored PDF open.
        tmp_path.unlink(missing_ok=True)
    else:
        tmp_path.replace(final_path)
    return final_path
