"""Privacy invariants for every tax fixture (plan WP2): parsed output never carries a full
SSN/EIN-shaped or 9-digit value, nor the recipient's name."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.tax_types import ParsedTaxDocument
from personal_finance_analyzer.ingest.tax import registry as tax_registry
from tests.fixtures.gen.tax_common import RECIPIENT_NAME, RECIPIENT_SSN, SPOUSE_NAME, SPOUSE_SSN
from tests.ingest.tax_support import GENERATED_DIR, extract_doc

NINE_DIGITS = re.compile(r"\d{9}")
SSN_SHAPE = re.compile(r"\d{3}-\d{2}-\d{4}")
TAX_PDFS = sorted(GENERATED_DIR.glob("tax_*/*.pdf"))

_NAME_PARTS = ("Jordan", "Testperson", *RECIPIENT_NAME.split(), *SPOUSE_NAME.split())
_SECRETS = (RECIPIENT_SSN, SPOUSE_SSN, RECIPIENT_SSN.replace("-", ""), SPOUSE_SSN.replace("-", ""))


def _outputs_for(pdf: Path) -> list[str]:
    """Everything a caller could see: parsed repr/json/warnings, or the error message."""
    doc = extract_doc(pdf)
    try:
        parsed = tax_registry.select_tax_parser(doc).parse(doc)
    except ParserError as exc:
        return [str(exc)]
    return _outputs(parsed)


def _outputs(parsed: ParsedTaxDocument) -> list[str]:
    warnings = list(parsed.warnings) + [w for f in parsed.forms for w in f.warnings]
    blob: dict[str, Any] = dataclasses.asdict(parsed)
    return [repr(parsed), json.dumps(blob, sort_keys=True, default=str), *warnings]


def test_fixtures_present() -> None:
    assert TAX_PDFS


@pytest.mark.parametrize("pdf", TAX_PDFS, ids=lambda p: p.stem)
def test_parsed_output_has_no_tin_shapes(pdf: Path) -> None:
    for text in _outputs_for(pdf):
        assert not NINE_DIGITS.search(text)
        assert not SSN_SHAPE.search(text)
        for secret in _SECRETS:
            assert secret not in text


@pytest.mark.parametrize("pdf", TAX_PDFS, ids=lambda p: p.stem)
def test_recipient_name_never_in_parsed_output(pdf: Path) -> None:
    for text in _outputs_for(pdf):
        for part in _NAME_PARTS:
            assert part.lower() not in text.lower()


def test_llm_fallback_output_has_no_tin_shapes() -> None:
    """Even when the model echoes identifiers, nothing identifier-shaped survives validation."""
    from personal_finance_analyzer.core.tax_types import UnparsedRegion
    from personal_finance_analyzer.llm.tax_parser import TaxLLMExtractor
    from tests.ingest.tax_support import doc_from_lines
    from tests.llm_fakes import FakeLLM, install, remote_cfg

    reply = json.dumps(
        {
            "forms": [
                {
                    "form_type": "1099-int",
                    "payer_name": f"Granite {RECIPIENT_SSN}",
                    "payer_tin_last4": "99-0000042",
                    "account_last4": RECIPIENT_SSN,
                    "pages": [1],
                    "items": [{"box": "1", "amount": "5.00"}],
                }
            ]
        }
    )
    mp = pytest.MonkeyPatch()
    try:
        install(mp, FakeLLM(default=reply))
        doc = doc_from_lines(["Tax Year 2024", "Payer 99-0000042", "Interest 5.00"])
        regions = (UnparsedRegion("unknown", (1,), "unreadable"),)
        result = TaxLLMExtractor(remote_cfg()).extract(doc, regions, tax_year=2024)
    finally:
        mp.undo()
    assert result.forms
    blob = repr(result)
    assert not NINE_DIGITS.search(blob) and not SSN_SHAPE.search(blob)
    assert RECIPIENT_SSN not in blob
