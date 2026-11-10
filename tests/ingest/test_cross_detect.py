"""Cross-lane `detect()` matrix (P1-Z, §2c intro, Gate 1).

Built from the already-merged fixtures of the four layout lanes (P1-B1..P1-B4):
``tests/fixtures/generated/{layout_a_credit,layout_b_credit,layout_c_credit,layout_d_bank}/``.

Two properties must hold across every layout parser and every fixture of every layout:

1. Each layout parser's own `detect()` scores its own fixtures above the registry's
   `DETECT_THRESHOLD` (``0.5``) and every *other* layout's fixtures at or below it — a lazy
   `detect()` (e.g. one that fires on a generic word like "Date" or "Amount") shows up here as a
   cross-layout score that should have been nowhere near the threshold.
2. `registry.select()`, using real (undiscovered-goes-through-normal-discovery) parser
   registration, routes every fixture to its own parser — not just that the *score* is right, but
   that the parser is actually reachable through the registry a real import goes through.
"""

from __future__ import annotations

from pathlib import Path

import pdfplumber
import pytest

from personal_finance_analyzer.core.types import ExtractedDoc, PageText, StatementParser, Word
from personal_finance_analyzer.ingest import registry
from personal_finance_analyzer.ingest.parsers.layout_a_credit import LayoutACredit
from personal_finance_analyzer.ingest.parsers.layout_b_credit import LayoutBCreditParser
from personal_finance_analyzer.ingest.parsers.layout_c_credit import LayoutCCreditParser
from personal_finance_analyzer.ingest.parsers.layout_d_bank import LayoutDBankParser

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "generated"

#: One direct instance per layout, independent of whatever the registry's module scan happens to
#: discover — this matrix is about each parser's own `detect()`, not about registration.
_PARSERS: dict[str, StatementParser] = {
    "layout_a_credit": LayoutACredit(),
    "layout_b_credit": LayoutBCreditParser(),
    "layout_c_credit": LayoutCCreditParser(),
    "layout_d_bank": LayoutDBankParser(),
}

#: Every variant every layout's generator produces (§2c common requirements: the same six
#: variants are generated for all four layouts, per `tests/fixtures/gen/`).
_VARIANTS = ("normal", "multiline", "fx", "refund_and_payment", "malformed", "no_summary")


def _extract_doc(pdf_path: Path) -> ExtractedDoc:
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            words = tuple(
                Word(text=w["text"], x0=w["x0"], x1=w["x1"], top=w["top"], bottom=w["bottom"])
                for w in page.extract_words()
            )
            text = page.extract_text() or ""
            pages.append(PageText(page_number=i, text=text, words=words, char_count=len(text)))
    doc_pages = tuple(pages)
    median_chars = sorted(p.char_count for p in doc_pages)[len(doc_pages) // 2] if doc_pages else 0
    return ExtractedDoc(
        file_sha256="test-sha",
        page_count=len(doc_pages),
        pages=doc_pages,
        has_text_layer=median_chars >= 50,
    )


def _fixture_path(layout_id: str, variant: str) -> Path:
    return _FIXTURES_DIR / layout_id / f"{layout_id}_{variant}.pdf"


_CASES = [
    (layout_id, variant)
    for layout_id in _PARSERS
    for variant in _VARIANTS
    if _fixture_path(layout_id, variant).exists()
]


# --------------------------------------------------------------------------------------------
# 1. detect() score matrix
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("layout_id", "variant"), _CASES)
def test_own_fixture_scores_above_threshold(layout_id: str, variant: str) -> None:
    doc = _extract_doc(_fixture_path(layout_id, variant))
    parser = _PARSERS[layout_id]
    score = parser.detect(doc)
    assert score > registry.DETECT_THRESHOLD, (
        f"{parser.id}.detect() scored its own fixture {layout_id}_{variant}.pdf at {score}, "
        f"not above the registry's threshold ({registry.DETECT_THRESHOLD})"
    )


@pytest.mark.parametrize(("layout_id", "variant"), _CASES)
def test_other_layouts_fixture_scores_at_or_below_threshold(layout_id: str, variant: str) -> None:
    doc = _extract_doc(_fixture_path(layout_id, variant))
    for other_id, other_parser in _PARSERS.items():
        if other_id == layout_id:
            continue
        score = other_parser.detect(doc)
        assert score <= registry.DETECT_THRESHOLD, (
            f"{other_parser.id}.detect() scored {layout_id}_{variant}.pdf (not its own layout) "
            f"at {score}, at or above the registry's threshold ({registry.DETECT_THRESHOLD}) — "
            "a lazy detect() that would mis-route this fixture away from its own parser"
        )


# --------------------------------------------------------------------------------------------
# 2. Registry routing: every fixture reaches its own parser through normal discovery, not just
#    a direct detect() call.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("layout_id", "variant"), _CASES)
def test_registry_routes_each_fixture_to_its_own_parser(layout_id: str, variant: str) -> None:
    doc = _extract_doc(_fixture_path(layout_id, variant))
    selected = registry.select(doc)
    assert selected.id == layout_id, (
        f"registry.select() routed {layout_id}_{variant}.pdf to {selected.id!r}, not to its own "
        f"parser {layout_id!r} — the registry's own discovery did not find (or did not prefer) "
        "the parser this fixture's detect() score says it should"
    )
