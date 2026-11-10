"""Statement page preview for the Layout mapper's click-to-map UI (§2c, local-only).

Reads page geometry (each word's `text`/`x0`/`x1`/`top`/`bottom`, plus the page's own size)
straight from a statement's staged PDF. `ingest.extract.extract` already reads word boxes but
not page dimensions, so this module opens the PDF a second time with `pdfplumber` (already a
frozen dependency, §0.3) just for `page.width`/`page.height`.

Stays on the localhost token API and never touches egress: this never imports anything from
`classify/llm/`, and the router this feeds returns geometry only, never a description string
that could be mistaken for something safe to send anywhere (I1b/I3).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import pdfplumber
import pypdfium2
from sqlalchemy.orm import Session

from personal_finance_analyzer.config import Settings
from personal_finance_analyzer.core.errors import PfaError
from personal_finance_analyzer.core.native_locks import PDFIUM_LOCK
from personal_finance_analyzer.core.types import Word
from personal_finance_analyzer.db.pii_terms import user_pii_terms
from personal_finance_analyzer.ingest.extract import extract


class PagePreviewNotFound(PfaError):
    """The requested page does not exist in this statement's PDF."""


@dataclass(frozen=True)
class PagePreview:
    page_number: int
    width: float
    height: float
    words: tuple[Word, ...]


def preview_page(pdf_path: Path, page_number: int) -> PagePreview:
    """Return `page_number`'s (1-based) words and page size from `pdf_path`.

    Raises:
        PagePreviewNotFound: `page_number` is out of range for this PDF.
    """
    doc = extract(pdf_path)
    page = next((p for p in doc.pages if p.page_number == page_number), None)
    if page is None:
        raise PagePreviewNotFound(
            f"page {page_number} does not exist (this statement has {doc.page_count} page(s))"
        )
    with pdfplumber.open(pdf_path) as pdf:
        if page_number < 1 or page_number > len(pdf.pages):
            raise PagePreviewNotFound(
                f"page {page_number} does not exist (this statement has {len(pdf.pages)} page(s))"
            )
        plumber_page = pdf.pages[page_number - 1]
        width, height = float(plumber_page.width), float(plumber_page.height)
    return PagePreview(page_number=page_number, width=width, height=height, words=page.words)


def page_infos(pdf_path: Path, selected: list[int] | None) -> list[tuple[int, int, str, bool]]:
    """``(page_number, char_count, snippet, selected)`` per page; ``selected=None`` = all."""
    doc = extract(pdf_path)
    chosen = set(selected) if selected else None
    return [
        (
            p.page_number,
            p.char_count,
            " ".join(p.text.split())[:300],
            chosen is None or p.page_number in chosen,
        )
        for p in doc.pages
    ]


def render_page_png(pdf_path: Path, page_number: int, width: int) -> bytes:
    """A PNG thumbnail of ``page_number`` (1-based), ``width`` pixels wide.

    pdfium is not thread-safe (the Import screen requests every thumbnail at once from FastAPI's
    threadpool; a crash is a hard SIGTRAP/SIGSEGV). So this drives `pypdfium2` directly and does
    everything, including closing the bitmap, page and document explicitly, under `PDFIUM_LOCK`,
    rather than leaving native handles to garbage-collector finalizers on arbitrary threads.

    Raises:
        PagePreviewNotFound: ``page_number`` is out of range.
    """
    with PDFIUM_LOCK:
        doc = pypdfium2.PdfDocument(pdf_path)
        page = None
        bitmap = None
        try:
            count = len(doc)
            if page_number < 1 or page_number > count:
                raise PagePreviewNotFound(
                    f"page {page_number} does not exist (this statement has {count} page(s))"
                )
            page = doc[page_number - 1]
            bitmap = page.render(scale=width / page.get_width())
            pil = bitmap.to_pil()
            buffer = io.BytesIO()
            pil.save(buffer, format="PNG")
            pil.close()
            return buffer.getvalue()
        finally:
            if bitmap is not None:
                bitmap.close()
            if page is not None:
                page.close()
            doc.close()


def llm_pii_terms(session: Session, settings: Settings, user_id: int | None) -> tuple[str, ...]:
    """The redaction terms the LLM statement parser uses (see `db.pii_terms.user_pii_terms`)."""
    return user_pii_terms(session, settings, user_id)
