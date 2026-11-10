"""Shared helpers for per-form tax parser tests.

    doc_from_lines(lines) -> ExtractedDoc          one page, words on a character grid
    doc_from_pages(pages) -> ExtractedDoc          several pages of lines
    extract_doc(pdf_path) -> ExtractedDoc          the real `ingest.extract.extract` (pdfplumber)
    generated_pdf(layout_id, variant) -> Path      tests/fixtures/generated/<id>/<id>_<variant>.pdf
    load_golden(layout_id, variant) -> dict        tests/ingest/golden/tax/<id>/<id>_<variant>.json
    parsed_to_dict(parsed) -> dict                 same as tax_common.golden_dict
    assert_matches_golden(parsed, golden)          compares forms/items and expected_warnings

`doc_from_lines`: each line is one row 12pt apart; every whitespace-separated token becomes a
`Word` at x = 6 * (character column), width 6 * len(token), glyph height 9. So leading and inner
spaces control x-alignment exactly (use that to build "below" layouts: a blank line is an empty
row, labels and values line up by column). `extract_doc` needs an isolated data home: the tests in
`tests/ingest/tax/` get one automatically from `conftest.py`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from personal_finance_analyzer.core.tax_types import ParsedTaxDocument
from personal_finance_analyzer.core.types import ExtractedDoc, PageText, Word
from personal_finance_analyzer.ingest.extract import extract
from tests.fixtures.gen.tax_common import golden_dict

CHAR_WIDTH = 6.0
LINE_HEIGHT = 12.0
GLYPH_HEIGHT = 9.0

_TESTS_DIR = Path(__file__).resolve().parent.parent
GENERATED_DIR = _TESTS_DIR / "fixtures" / "generated"
GOLDEN_DIR = _TESTS_DIR / "ingest" / "golden" / "tax"

_TOKEN = re.compile(r"\S+")


def _page(lines: Sequence[str], page_number: int) -> PageText:
    words: list[Word] = []
    for row, line in enumerate(lines):
        top = row * LINE_HEIGHT
        for match in _TOKEN.finditer(line):
            words.append(
                Word(
                    text=match.group(0),
                    x0=match.start() * CHAR_WIDTH,
                    x1=match.end() * CHAR_WIDTH,
                    top=top,
                    bottom=top + GLYPH_HEIGHT,
                )
            )
    text = "\n".join(line.rstrip() for line in lines)
    return PageText(page_number=page_number, text=text, words=tuple(words), char_count=len(text))


def doc_from_pages(pages: Sequence[Sequence[str]]) -> ExtractedDoc:
    """An `ExtractedDoc` with one `PageText` per entry of `pages` (see module docstring)."""
    built = tuple(_page(lines, i) for i, lines in enumerate(pages, start=1))
    return ExtractedDoc(
        file_sha256="test-sha", page_count=len(built), pages=built, has_text_layer=True
    )


def doc_from_lines(lines: Sequence[str]) -> ExtractedDoc:
    """A single-page `ExtractedDoc` laid out on a character grid (see module docstring)."""
    return doc_from_pages([lines])


def extract_doc(pdf_path: Path) -> ExtractedDoc:
    """Extract `pdf_path` with the production extractor (word boxes included)."""
    return extract(pdf_path)


def generated_pdf(layout_id: str, variant: str) -> Path:
    """Path of a generated fixture PDF."""
    return GENERATED_DIR / layout_id / f"{layout_id}_{variant}.pdf"


def load_golden(layout_id: str, variant: str) -> dict[str, Any]:
    """Load the frozen golden JSON for `layout_id`/`variant`."""
    path = GOLDEN_DIR / layout_id / f"{layout_id}_{variant}.json"
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def parsed_to_dict(parsed: ParsedTaxDocument) -> dict[str, Any]:
    """`ParsedTaxDocument` -> JSON-able dict (same as `tax_common.golden_dict`)."""
    return golden_dict(parsed)


def assert_matches_golden(parsed: ParsedTaxDocument, golden: dict[str, Any]) -> None:
    """Assert `parsed` equals `golden` (a dict from `build_golden`/`load_golden`).

    The ``expected_warnings`` key, when present, is compared against the sorted union of the
    document-level and form-level warnings; everything else must equal `parsed_to_dict(parsed)`.
    """
    expected = {k: v for k, v in golden.items() if k != "expected_warnings"}
    actual = parsed_to_dict(parsed)
    assert actual == expected
    if "expected_warnings" in golden:
        warnings = list(parsed.warnings) + [w for form in parsed.forms for w in form.warnings]
        assert sorted(warnings) == sorted(golden["expected_warnings"])
