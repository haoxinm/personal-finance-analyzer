"""PDF text-layer extraction and text-layer detection (P1-A, §3.1, §3.12a).

`extract()` is the single place a PDF's bytes are turned into an `ExtractedDoc`: plain text per
page plus word bounding boxes, read once with `pdfplumber` (built on `pdfminer.six`). Everything
downstream — issuer detection, parser scoring, and every `StatementParser.parse()` — consumes an
`ExtractedDoc`, never the PDF file itself.

Text-layer detection (whether the PDF is a scanned/image-only document) is a *report*, not a
raise: a caller decides what to do with `has_text_layer=False` (P2-A maps it to
``status='no_text_layer'``). The one condition this module does raise for is a PDF it cannot open
at all: a password-protected file.

No network, ever (I3): this module opens no socket and imports no HTTP client.
"""

from __future__ import annotations

import hashlib
import statistics
from pathlib import Path

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException

from personal_finance_analyzer.config import load_settings
from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.paths import ensure_home, extract_cache_dir
from personal_finance_analyzer.core.types import TEXT_LAYER_MIN_CHARS, ExtractedDoc, PageText, Word

#: Bytes read per chunk while hashing a statement file. Large enough to be fast, small enough to
#: never hold a whole multi-page PDF in memory twice.
_HASH_CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of the file at ``path``.

    Reads the file in fixed-size chunks rather than loading it whole, so it scales to large
    statements. This digest is the statement's stable identity: the stored copy's filename
    (A12), the dedupe/no-op-reimport key (I9), and the debug extract-cache key.
    """
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def extract(path: Path, *, write_cache: bool = True) -> ExtractedDoc:
    """Extract ``path``'s text layer: per-page plain text and word bounding boxes.

    Opens the PDF exactly once with `pdfplumber` and reads every page's `extract_text()` and
    `extract_words()` while the file is open, so a large statement is never re-opened per page.
    Word coordinates (`Word.x0`/`x1`/`top`/`bottom`) are kept because statement tables are
    visually aligned: column x-ranges are the most reliable signal `ingest/layout.py` has for
    locating a table and its columns.

    Sets `ExtractedDoc.has_text_layer` to `False` when the *median* page has fewer than
    `TEXT_LAYER_MIN_CHARS` characters — a scanned/image-only document. This is reported, not
    raised: the caller (P2-A) turns it into `statements.status = 'no_text_layer'`.

    If `[privacy] store_extract_cache` is enabled in settings, writes the joined page text to
    ``extract_cache/<file_sha256>.txt`` under the data home, for debugging only. Off by default,
    and never required for correct operation.

    Args:
        path: path to a local PDF file.
        write_cache: when False, never write the debug cache even if `store_extract_cache` is
            on. Tax-form imports pass False: the cache would hold full SSNs/EINs on disk.

    Returns:
        The extracted document.

    Raises:
        ParserError: the PDF is password-protected (or otherwise fails to open/decrypt), with an
            actionable message for the user.
    """
    file_sha256 = sha256_file(path)

    try:
        with pdfplumber.open(path) as pdf:
            pages = tuple(
                _extract_page(page, page_number=i) for i, page in enumerate(pdf.pages, start=1)
            )
    except PdfminerException as exc:
        raise _to_parser_error(exc) from exc

    char_counts = [page.char_count for page in pages]
    median_chars = statistics.median(char_counts) if char_counts else 0.0
    has_text_layer = median_chars >= TEXT_LAYER_MIN_CHARS

    doc = ExtractedDoc(
        file_sha256=file_sha256,
        page_count=len(pages),
        pages=pages,
        has_text_layer=has_text_layer,
    )
    if write_cache:
        _maybe_write_debug_cache(doc)
    return doc


def _extract_page(page: pdfplumber.page.Page, *, page_number: int) -> PageText:
    text = page.extract_text() or ""
    words = tuple(
        Word(text=w["text"], x0=w["x0"], x1=w["x1"], top=w["top"], bottom=w["bottom"])
        for w in page.extract_words()
    )
    return PageText(page_number=page_number, text=text, words=words, char_count=len(text))


def _to_parser_error(exc: PdfminerException) -> ParserError:
    cause = exc.args[0] if exc.args else None
    if isinstance(cause, PDFPasswordIncorrect):
        return ParserError(
            "This PDF is password-protected and could not be opened. Remove the password "
            "(e.g. print to a new, unprotected PDF) and re-upload the file."
        )
    return ParserError("This PDF could not be read; it may be corrupted.")


def _maybe_write_debug_cache(doc: ExtractedDoc) -> None:
    settings = load_settings()
    if not settings.privacy.store_extract_cache:
        return
    ensure_home()
    cache_path = extract_cache_dir() / f"{doc.file_sha256}.txt"
    cache_path.write_text(doc.full_text, encoding="utf-8")
