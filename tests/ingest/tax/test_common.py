from __future__ import annotations

import pytest

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.tax_types import ParsedTaxForm
from personal_finance_analyzer.ingest.tax import common
from tests.ingest.tax_support import doc_from_lines


def _form(year: int = 2024, **kw: object) -> ParsedTaxForm:
    base: dict[str, object] = {
        "form_type": "w2",
        "tax_year": year,
        "payer_name": None,
        "payer_tin_last4": None,
        "account_last4": None,
        "is_corrected": False,
        "filing_status": None,
        "agi_minor": None,
        "taxable_income_minor": None,
        "income": (),
        "taxes": (),
    }
    base.update(kw)
    return ParsedTaxForm(**base)  # type: ignore[arg-type]


def test_require_year_override_and_detection() -> None:
    doc = doc_from_lines(["Tax Year 2023"])
    assert common.require_year(doc, 2020) == 2020
    assert common.require_year(doc, None) == 2023


def test_require_year_errors_are_generic() -> None:
    with pytest.raises(ParserError) as excinfo:
        common.require_year(doc_from_lines(["no year 912-34-5678"]), None)
    assert "912" not in str(excinfo.value)
    with pytest.raises(ParserError):
        common.require_year(doc_from_lines(["Tax Year 2023"]), 1850)


def test_sanitize_payer_name() -> None:
    assert common.sanitize_payer_name("  Pinecrest   Logistics LLC ") == "Pinecrest Logistics LLC"
    assert common.sanitize_payer_name("912-34-5678") is None
    assert common.sanitize_payer_name("912345678") is None
    assert common.sanitize_payer_name("9 1 2 3 4 5 6 7 8") is None
    assert common.sanitize_payer_name("   ") is None
    assert common.sanitize_payer_name(None) is None
    assert common.sanitize_payer_name("Acme 99-0000001") == "Acme **-***0001"
    assert len(common.sanitize_payer_name("x" * 500) or "") == 120


def test_build_document_year_agreement() -> None:
    doc = common.build_document([_form(2024), _form(2024)], warnings=["a", "a", "b"])
    assert doc.tax_year == 2024
    assert doc.warnings == ("a", "b")
    with pytest.raises(ParserError):
        common.build_document([_form(2024), _form(2023)])
    with pytest.raises(ParserError):
        common.build_document([])


def test_build_document_privacy_guards() -> None:
    with pytest.raises(ValueError):
        common.build_document([_form(payer_tin_last4="99-0000001")])
    with pytest.raises(ValueError):
        common.build_document([_form(warnings=("value 1234567 seen",))])
    with pytest.raises(ValueError):
        common.build_document([_form()], warnings=["tin 912-34-5678"])


def test_1099_section_detection() -> None:
    doc = doc_from_lines(["Form 1099-INT", "Interest", "1099-DIV Dividends"])
    assert common.distinct_1099_sections(doc) == {"INT", "DIV"}
    assert common.other_1099_section_present(doc, "INT")
    assert not common.other_1099_section_present(doc_from_lines(["Form 1099-INT"]), "INT")
    nec = doc_from_lines(["Form 1099-NEC", "Form 1099-MISC"])
    assert not common.other_1099_section_present(nec, ("NEC", "MISC"))
    assert common.halve_if_other_1099(0.9, doc, "INT") == pytest.approx(0.45)
    assert common.halve_if_other_1099(0.9, doc_from_lines(["Form 1099-INT"]), "INT") == 0.9


def test_helper_modules_expose_no_parser_shaped_objects() -> None:
    from personal_finance_analyzer.ingest.tax import fields, tin, year

    for module in (common, fields, tin, year):
        for name in dir(module):
            obj = getattr(module, name)
            if isinstance(obj, type):
                continue
            assert not (hasattr(obj, "form_types") and hasattr(obj, "detect")), (module, name)
