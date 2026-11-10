"""Local-only issuer match preview for the issuer/onboarding screens (A24).

This module only ever calls `ingest.extract.extract`, `ingest.layout.locate_table_bands`, and
`ingest.issuer_match.non_table_page_one_text` (all already used elsewhere in this codebase, and
all local-only per their own docstrings) against PDFs already staged on disk, and returns only
statement metadata (id, status, original name, period) — never page text — to the API layer.
Never imports anything from `classify/llm/` (I1b/I3).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.api.services.crud import staged_pdf_path
from personal_finance_analyzer.db.models import Statement
from personal_finance_analyzer.ingest.extract import extract
from personal_finance_analyzer.ingest.issuer_match import non_table_page_one_text
from personal_finance_analyzer.ingest.layout import locate_table_bands

#: Mirrors `ingest.issuer_match._normalize` exactly (casefold, strip punctuation, collapse
#: whitespace, then again with spaces removed), duplicated locally rather than imported so this
#: module depends only on `ingest.issuer_match`'s public `non_table_page_one_text` — the private
#: `_normalize` stays P2-A's to change freely.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> tuple[str, str]:
    spaced = _WS_RE.sub(" ", _PUNCT_RE.sub(" ", text.casefold())).strip()
    compact = spaced.replace(" ", "")
    return spaced, compact


@dataclass(frozen=True)
class MatchPreviewRow:
    statement_id: int
    original_name: str
    status: str
    period_start: str | None
    period_end: str | None


def preview_match(session: Session, match_terms: Sequence[str]) -> list[MatchPreviewRow]:
    """Return every statement whose page-1, non-table text would match one of `match_terms`
    (§2f.4's own longest-match tie-breaking isn't needed here — a preview only needs to know
    *whether* a term would match at all, not which of several candidate issuers would win were
    they all configured at once).

    Skips a statement with no staged PDF on disk, or one whose PDF fails to re-extract, rather
    than erroring the whole preview over one bad statement.
    """
    term_forms = [_normalize(t) for t in match_terms if t.strip()]
    if not term_forms:
        return []

    rows: list[MatchPreviewRow] = []
    for statement in session.execute(select(Statement)).scalars().all():
        pdf_path = staged_pdf_path(statement)
        if pdf_path is None:
            continue
        try:
            doc = extract(pdf_path)
        except Exception:  # a corrupt/unreadable staged PDF just isn't previewable
            continue
        table_bands = locate_table_bands(doc)
        text = non_table_page_one_text(doc, table_bands)
        spaced_text, compact_text = _normalize(text)
        if not spaced_text:
            continue
        matched = any(
            (term_spaced and term_spaced in spaced_text)
            or (term_compact and term_compact in compact_text)
            for term_spaced, term_compact in term_forms
        )
        if matched:
            rows.append(
                MatchPreviewRow(
                    statement_id=statement.id,
                    original_name=statement.original_name,
                    status=statement.status,
                    period_start=statement.period_start,
                    period_end=statement.period_end,
                )
            )
    return rows
