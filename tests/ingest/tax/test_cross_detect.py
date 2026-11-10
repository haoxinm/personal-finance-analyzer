"""Cross-detection matrix between tax parsers, tax fixtures and bank statement parsers/fixtures.

* Every generated tax fixture scores > 0.5 on its own parser and <= 0.5 on every other tax
  parser, and `select_tax_parser` routes to it.
* No bank statement fixture scores above the threshold on any tax parser, and no bank parser
  scores above the threshold on any tax fixture (a tax PDF is never picked as a statement).
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest import registry as bank_registry
from personal_finance_analyzer.ingest.tax import registry as tax_registry
from tests.ingest.tax_support import GENERATED_DIR, extract_doc

THRESHOLD = tax_registry.TAX_DETECT_THRESHOLD


def _pdfs(*, tax: bool) -> list[Path]:
    found = [
        p for p in sorted(GENERATED_DIR.glob("*/*.pdf")) if p.parent.name.startswith("tax_") == tax
    ]
    assert found
    return found


ALL_TAX_PDFS = _pdfs(tax=True)
_PARSER_IDS = {p.id for p in tax_registry.discover_tax_parsers()}
#: Fixtures that exist for exactly one parser. The statement / mixed-package fixtures exercise
#: segmentation across parsers (see `test_orchestrator.py`) and have no single owner.
TAX_PDFS = [p for p in ALL_TAX_PDFS if p.parent.name in _PARSER_IDS]
BANK_PDFS = _pdfs(tax=False)


@cache
def _doc(path: Path) -> ExtractedDoc:
    return extract_doc(path)


def _id(path: Path) -> str:
    return path.stem


@pytest.mark.parametrize("pdf", TAX_PDFS, ids=_id)
def test_tax_fixture_scores_only_on_own_parser(pdf: Path) -> None:
    doc = _doc(pdf)
    scores = tax_registry.classify_form(doc).scores
    own = pdf.parent.name
    assert own in scores
    assert scores[own] > THRESHOLD, scores
    others = {pid: s for pid, s in scores.items() if pid != own}
    assert all(s <= THRESHOLD for s in others.values()), scores


@pytest.mark.parametrize("pdf", TAX_PDFS, ids=_id)
def test_select_tax_parser_routes_to_own_parser(pdf: Path) -> None:
    assert tax_registry.select_tax_parser(_doc(pdf)).id == pdf.parent.name


def test_every_tax_parser_has_fixtures() -> None:
    ids = {p.id for p in tax_registry.discover_tax_parsers()}
    assert ids == {p.parent.name for p in TAX_PDFS}


@pytest.mark.parametrize("pdf", BANK_PDFS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_bank_fixture_scores_low_on_every_tax_parser(pdf: Path) -> None:
    if pdf.stem.endswith("_encrypted"):
        pytest.skip("password-protected fixture has no extractable text")
    doc = _doc(pdf)
    for parser in tax_registry.discover_tax_parsers():
        assert parser.detect(doc) <= THRESHOLD, parser.id


@pytest.mark.parametrize("pdf", ALL_TAX_PDFS, ids=_id)
def test_tax_fixture_scores_low_on_every_bank_parser(pdf: Path) -> None:
    doc = _doc(pdf)
    parsers = bank_registry.discover_builtin_parsers()
    assert parsers
    for parser in parsers:
        assert parser.detect(doc) <= THRESHOLD, parser.id
