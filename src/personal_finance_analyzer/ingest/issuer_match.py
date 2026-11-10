"""Issuer detection by template matching (§2f.4, A24).

A plain substring search over ``issuers.match_terms`` — deliberately so: inspectable, debuggable,
fixable by editing one list. It is scoped to the **non-table region of page 1** (`match_issuer`
reuses `layout.locate_table_bands`, P0-7) because bank names appear constantly *inside*
transaction descriptions (``PAYMENT TO CHASE``, ``ZELLE FROM BOFA``); a whole-document search
would confidently misattribute the issuer of a statement that merely mentions another bank.

Matching normalizes both the page text and every candidate term to two forms — casefolded,
punctuation-stripped, whitespace-collapsed (*spaced*) and additionally space-removed (*compact*) —
so a user-entered ``jp morgan chase`` matches a printed ``JPMorgan Chase`` even though the two
disagree about where the space goes; only the compact form lines up in that case. The longest
matching term (by either form) wins; a tie between two different issuers' longest matches is
**not** resolved — an ambiguous statement gets no auto-selected issuer (§2f.4's ``jpmorgan chase``
vs. bare ``chase`` example, and the "two issuers at comparable length" case).

This module is local-only twice over: it never imports an HTTP client (I3) and it never sends an
issuer name or `match_terms` anywhere (I1b) — matching happens entirely on this machine.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.db.models import Issuer
from personal_finance_analyzer.ingest.layout import TableBand, cluster_rows

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> tuple[str, str]:
    """Return ``(spaced, compact)``: casefolded, punctuation-stripped, whitespace-collapsed, and
    that same result again with every space removed."""
    spaced = _WS_RE.sub(" ", _PUNCT_RE.sub(" ", text.casefold())).strip()
    compact = spaced.replace(" ", "")
    return spaced, compact


def non_table_page_one_text(doc: ExtractedDoc, table_bands: Sequence[TableBand]) -> str:
    """Return page 1's text, excluding every row inside a located table band (§2f.4 step 2).

    Falls back to the full page-1 text when no table band was located on it (an unusual
    statement with no recognizable table on its first page): searching the whole page is still
    safer than refusing to match at all, and no table means no in-description false positives to
    guard against in the first place.
    """
    page_one = next((p for p in doc.pages if p.page_number == 1), None)
    if page_one is None:
        return ""
    excluded_rows = {
        row_index for band in table_bands if band.page_number == 1 for row_index in band.row_indices
    }
    if not excluded_rows:
        return page_one.text
    rows = cluster_rows(page_one.words)
    return "\n".join(row.text for i, row in enumerate(rows) if i not in excluded_rows)


def match_issuer(
    session: Session, doc: ExtractedDoc, table_bands: Sequence[TableBand]
) -> int | None:
    """Return the `issuers.id` whose `match_terms` (or name) has the longest substring match in
    ``doc``'s page-1, non-table text, or `None` when nothing matches or two issuers tie for the
    longest match (§2f.4, A24).

    Never raises: an issuer with unparsable `match_terms` JSON is skipped rather than aborting
    matching for every other issuer.
    """
    text = non_table_page_one_text(doc, table_bands)
    spaced_text, compact_text = _normalize(text)
    if not spaced_text:
        return None

    scored: list[tuple[int, int]] = []  # (longest matched term length, issuer_id)
    for issuer in session.execute(select(Issuer)).scalars():
        best_len = _best_match_length(issuer, spaced_text, compact_text)
        if best_len > 0:
            scored.append((best_len, issuer.id))

    if not scored:
        return None
    scored.sort(reverse=True)
    top_len, top_id = scored[0]
    if len(scored) > 1 and scored[1][0] == top_len:
        return None  # ambiguous: two issuers matched at the same (longest) length
    return top_id


def _best_match_length(issuer: Issuer, spaced_text: str, compact_text: str) -> int:
    try:
        terms = json.loads(issuer.match_terms)
    except (json.JSONDecodeError, TypeError):
        terms = []
    candidates = {issuer.name, *terms}

    best = 0
    for term in candidates:
        term_spaced, term_compact = _normalize(str(term))
        if term_spaced and term_spaced in spaced_text:
            best = max(best, len(term_spaced))
        if term_compact and term_compact in compact_text:
            best = max(best, len(term_compact))
    return best
