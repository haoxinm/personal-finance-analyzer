"""Tripwire for synthetic tax fixtures (WP3): invented identities only.

Tolerant of there being no tax builders yet: it scans whatever `tests/fixtures/gen/tax_*.py`
modules and `tests/fixtures/generated/tax_*/` outputs exist (`tax_common.py` always does).
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

from tests.generate_fixtures import _looks_like_builder

_FIXTURES = Path(__file__).resolve().parent
_GEN_DIR = _FIXTURES / "gen"
_GENERATED_DIR = _FIXTURES / "generated"
_REPO = _FIXTURES.parent.parent
_GEN_FILES = sorted(_GEN_DIR.glob("tax_*.py"))

_SSN_HYPHEN = re.compile(r"(?<!\d)(\d{3})-(\d{2})-(\d{4})(?!\d)")
_BARE_NINE = re.compile(r"(?<![\d.,-])(\d{9})(?![\d.,-])")
_EIN = re.compile(r"(?<![\d-])(\d{2})-(\d{7})(?!\d)")
_STATEMENT_NAME = re.compile(r"(?i)(^|[/_\-\s])(statement|estatement|billing|e-?bill)([/_\-\s.]|$)")

#: Well-known real institutions that must never appear in synthetic tax fixtures.
_DENYLIST = (
    "chase",
    "wells fargo",
    "bank of america",
    "citibank",
    "citigroup",
    "capital one",
    "american express",
    "discover",
    "fidelity",
    "vanguard",
    "schwab",
    "td ameritrade",
    "e*trade",
    "etrade",
    "robinhood",
    "merrill",
    "morgan stanley",
    "goldman sachs",
    "jpmorgan",
    "us bank",
    "pnc",
    "ally bank",
    "t. rowe",
    "blackrock",
    "paychex",
    "gusto",
    "adp",
    "amazon",
    "walmart",
    "google",
    "microsoft",
    "apple inc",
)


def _ssn_area_is_invalid(area: str) -> bool:
    return area.startswith("9") or area in {"000", "666"}


def _problems_in(text: str) -> list[str]:
    problems: list[str] = []
    for match in _SSN_HYPHEN.finditer(text):
        if not _ssn_area_is_invalid(match.group(1)):
            problems.append(f"SSN-shaped value with a valid area number: {match.group(1)}-xx-xxxx")
    for match in _BARE_NINE.finditer(text):
        if not _ssn_area_is_invalid(match.group(1)[:3]):
            problems.append(
                f"9-digit value with a valid SSN area number: {match.group(1)[:3]}xxxxxx"
            )
    for match in _EIN.finditer(text):
        if match.group(1) != "99":
            problems.append(
                f"EIN-shaped value without the invented 99 prefix: {match.group(1)}-xxxxxxx"
            )
    lowered = text.lower()
    for name in _DENYLIST:
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", lowered):
            problems.append(f"real institution name: {name!r}")
    return problems


def test_scanner_flags_what_it_should() -> None:
    assert _problems_in("ssn 123-45-6789")
    assert _problems_in("ein 12-3456789")
    assert _problems_in("Wells Fargo")
    assert _problems_in("id 123456789")
    assert not _problems_in("ssn 912-34-5678 ein 99-0000001 666-12-3456 000-12-3456 acct 912345678")
    assert not _problems_in("Harbor Mill Savings, Copperfield Securities, Pinecrest Logistics LLC")


def test_tax_common_always_present() -> None:
    assert (_GEN_DIR / "tax_common.py") in _GEN_FILES


@pytest.mark.parametrize("path", _GEN_FILES, ids=lambda p: p.name)
def test_generator_sources_use_only_invented_identifiers(path: Path) -> None:
    assert _problems_in(path.read_text()) == []


@pytest.mark.parametrize("path", _GEN_FILES, ids=lambda p: p.name)
def test_generator_module_conventions(path: Path) -> None:
    module = importlib.import_module(f"tests.fixtures.gen.{path.stem}")
    if path.stem == "tax_common":
        for attr in dir(module):
            assert not _looks_like_builder(getattr(module, attr)), attr
        return
    builder = getattr(module, "BUILDER", None)
    assert builder is not None, "tax generator modules must export BUILDER"
    assert _looks_like_builder(builder)
    assert builder.layout_id == path.stem
    assert not _STATEMENT_NAME.search(builder.layout_id)


def test_tax_common_recipient_and_ids_are_invalid_by_construction() -> None:
    from tests.fixtures.gen import tax_common

    for ssn in (tax_common.RECIPIENT_SSN, tax_common.SPOUSE_SSN):
        match = _SSN_HYPHEN.fullmatch(ssn)
        assert match is not None and _ssn_area_is_invalid(match.group(1))
    for entity in (
        tax_common.EMPLOYER_A,
        tax_common.EMPLOYER_B,
        tax_common.BANK,
        tax_common.BROKER,
    ):
        assert entity.ein.startswith("99-")
        assert _problems_in(entity.name) == []


_GENERATED_FILES = sorted(p for p in _GENERATED_DIR.glob("tax_*/*") if p.is_file())


@pytest.mark.parametrize("path", _GENERATED_FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_generated_files_are_clean(path: Path) -> None:
    rel = path.relative_to(_REPO).as_posix()
    assert not _STATEMENT_NAME.search(rel)
    if path.suffix == ".json":
        json.loads(path.read_text())
        assert _problems_in(path.read_text()) == []
    elif path.suffix == ".pdf":
        from personal_finance_analyzer.ingest.extract import extract

        assert _problems_in(extract(path).full_text) == []
