"""Temporary copy of a tax PDF that the user selected again to read pages with the LLM.

Privacy default: the PDF of an imported tax document is deleted (``[privacy] store_tax_pdf_copies``
off). To use the page picker / thumbnails / LLM read afterwards the user re-selects the same file;
the router checks its sha256 against the document's and stages it here, only for the session's
need:

- one file per document, ``<tax_staging_dir>/<document_id>.pdf`` (mode 0600) in a private
  directory (mode 0700) that `ensure_home` does not create and no export or backup reads;
- it expires `STAGING_TTL_SECONDS` after it was written (the file's mtime is the clock, so there is
  no extra state to persist); an expired file is deleted when it is looked up, when anything is
  staged and when the app starts (`purge_all`: nothing outlives the process);
- it is removed after a successful LLM read, when the document is deleted, and on expiry.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from personal_finance_analyzer.core.paths import tax_staging_dir

#: How long a re-selected PDF stays available.
STAGING_TTL_SECONDS = 30 * 60


def _path(document_id: int) -> Path:
    return tax_staging_dir() / f"{document_id}.pdf"


def _expires_epoch(path: Path) -> float | None:
    try:
        return path.stat().st_mtime + STAGING_TTL_SECONDS
    except OSError:
        return None


def purge_expired() -> None:
    """Delete every expired staged file."""
    directory = tax_staging_dir()
    if not directory.is_dir():
        return
    now = time.time()
    for path in directory.glob("*.pdf"):
        expires = _expires_epoch(path)
        if expires is None or expires <= now:
            path.unlink(missing_ok=True)


def purge_all() -> None:
    """Delete every staged file (app start: nothing outlives the process)."""
    directory = tax_staging_dir()
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if path.is_file():
            path.unlink(missing_ok=True)


def stage(document_id: int, data: bytes) -> Path:
    """Write `data` as the staged copy of `document_id` (replacing any earlier one)."""
    directory = tax_staging_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        directory.chmod(stat.S_IRWXU)
    purge_expired()
    fd, tmp_name = tempfile.mkstemp(dir=directory, suffix=".tmp")  # created 0600
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(data)
        final = _path(document_id)
        Path(tmp_name).replace(final)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return final


def get(document_id: int) -> Path | None:
    """The staged PDF of `document_id`, or `None` (also when it has expired, which deletes it)."""
    path = _path(document_id)
    expires = _expires_epoch(path)
    if expires is None:
        return None
    if expires <= time.time():
        path.unlink(missing_ok=True)
        return None
    return path


def expires_at(document_id: int) -> str | None:
    """ISO-8601 UTC expiry of the staged copy, or `None` when there is none."""
    if get(document_id) is None:
        return None
    expires = _expires_epoch(_path(document_id))
    if expires is None:
        return None
    return datetime.fromtimestamp(expires, tz=UTC).isoformat()


def discard(document_id: int) -> None:
    _path(document_id).unlink(missing_ok=True)
