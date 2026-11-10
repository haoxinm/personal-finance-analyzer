from __future__ import annotations

import pytest

from personal_finance_analyzer.core.errors import UnsupportedLayoutError
from personal_finance_analyzer.core.tax_types import FormType, ParsedTaxDocument
from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest import registry as bank_registry
from personal_finance_analyzer.ingest.tax import registry
from tests.ingest.tax_support import doc_from_lines


class _Dummy:
    version = "1"

    def __init__(self, pid: str, score: float, forms: tuple[FormType, ...] = ("w2",)) -> None:
        self.id = pid
        self.form_types = forms
        self._score = score

    def detect(self, doc: ExtractedDoc) -> float:
        return self._score

    def parse(self, doc: ExtractedDoc, *, tax_year: int | None = None) -> ParsedTaxDocument:
        raise NotImplementedError


class _Raising(_Dummy):
    def detect(self, doc: ExtractedDoc) -> float:
        raise RuntimeError("boom")


DOC = doc_from_lines(["Tax Year 2024", "hello"])


def test_classify_picks_best_and_reports_scores() -> None:
    a, b = _Dummy("a", 0.6), _Dummy("b", 0.9, ("1099-int",))
    result = registry.classify_form(DOC, [a, b])
    assert result.form_type == "1099-int"
    assert result.parser_id == "b"
    assert result.confidence == 0.9
    assert result.tax_year == 2024
    assert dict(result.scores) == {"a": 0.6, "b": 0.9}


def test_threshold_is_strict() -> None:
    assert registry.TAX_DETECT_THRESHOLD == 0.5
    result = registry.classify_form(DOC, [_Dummy("a", 0.5)])
    assert result.form_type is None
    assert result.parser_id is None
    assert result.confidence == 0.5
    with pytest.raises(UnsupportedLayoutError):
        registry.select_tax_parser(DOC, [_Dummy("a", 0.5)])


def test_multi_form_parser_leaves_form_type_none() -> None:
    result = registry.classify_form(DOC, [_Dummy("nm", 0.9, ("1099-nec", "1099-misc"))])
    assert result.form_type is None
    assert result.parser_id == "nm"


def test_select_returns_highest_and_first_on_tie() -> None:
    a, b, c = _Dummy("a", 0.8), _Dummy("b", 0.8), _Dummy("c", 0.7)
    assert registry.select_tax_parser(DOC, [a, b, c]) is a
    assert registry.select_tax_parser(DOC, [c, _Dummy("d", 0.95)]).id == "d"


def test_raising_and_bad_detect_score_zero() -> None:
    result = registry.classify_form(
        DOC, [_Raising("r", 0.0), _Dummy("nan", float("nan")), _Dummy("big", 7.0)]
    )
    assert result.scores["r"] == 0.0
    assert result.scores["nan"] == 0.0
    assert result.scores["big"] == 1.0


def test_unsupported_message_has_no_document_text() -> None:
    doc = doc_from_lines(["SECRET-TEXT-123456789"])
    with pytest.raises(UnsupportedLayoutError) as excinfo:
        registry.select_tax_parser(doc, [_Dummy("a", 0.1)])
    assert "SECRET" not in str(excinfo.value)


def test_no_parsers_raises() -> None:
    with pytest.raises(UnsupportedLayoutError):
        registry.select_tax_parser(DOC, [])
    assert registry.classify_form(DOC, []).confidence == 0.0


def test_discover_tax_parsers_returns_only_tax_parsers() -> None:
    found = registry.discover_tax_parsers()
    ids = [p.id for p in found]
    assert ids == sorted(set(ids))


def test_bank_registry_contains_no_tax_parser() -> None:
    tax_ids = {p.id for p in registry.discover_tax_parsers()}
    bank_ids = {p.id for p in bank_registry.discover_all_parsers()}
    assert not tax_ids & bank_ids
    assert all(not pid.startswith("tax_") for pid in bank_ids)
    for parser in bank_registry.discover_all_parsers():
        assert not hasattr(parser, "form_types")


def test_tax_parsers_never_look_like_statement_parsers() -> None:
    for parser in registry.discover_tax_parsers():
        assert not bank_registry._looks_like_parser(parser)
