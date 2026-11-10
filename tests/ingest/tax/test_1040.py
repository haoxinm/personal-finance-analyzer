"""Tests for the Form 1040 parser (`tax_1040`).

Generated PDFs live in `tests/fixtures/generated/tax_1040/` (builder: `tests/fixtures/gen/
tax_1040.py`); the frozen reference goldens are under `tests/ingest/golden/tax/tax_1040/`.
In-memory documents (built from the builder's own page text via `doc_from_pages`) cover layout
quirks and consistency warnings that no fixture PDF exercises.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict

import pytest

from personal_finance_analyzer.core.errors import ParserError, UnsupportedLayoutError
from personal_finance_analyzer.core.tax_types import ParsedTaxDocument, ParsedTaxForm, TaxFormParser
from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest.tax import estimate
from personal_finance_analyzer.ingest.tax.parsers.f1040 import (
    F1040_FIELDS,
    PARSER,
    SUPPORTED_1040_YEARS,
)
from tests.fixtures.gen.tax_1040 import BUILDER, _calc, _pages, _Spec, spec_for
from tests.ingest.tax_support import (
    assert_matches_golden,
    doc_from_pages,
    extract_doc,
    generated_pdf,
    load_golden,
)

LAYOUT_ID = "tax_1040"
VARIANTS = BUILDER.variants
_LONG_DIGITS = re.compile(r"\d{6,}")


def _parse_fixture(variant: str) -> ParsedTaxDocument:
    return PARSER.parse(extract_doc(generated_pdf(LAYOUT_ID, variant)))


def _form(variant: str) -> ParsedTaxForm:
    (form,) = _parse_fixture(variant).forms
    return form


def _amounts(form: ParsedTaxForm) -> dict[str, int]:
    return {t.category: t.amount_minor for t in form.taxes}


def _income(form: ParsedTaxForm) -> dict[str, int]:
    return {i.category: i.amount_minor for i in form.income}


def _mem_doc(
    variant: str = "normal_wages_only",
    edit: Callable[[list[list[str]]], list[list[str]]] | None = None,
) -> ExtractedDoc:
    spec, calc = spec_for(variant)
    pages = _pages(spec, calc)
    if edit is not None:
        pages = edit(pages)
    return doc_from_pages(pages)


def _replace(old: str, new: str) -> Callable[[list[list[str]]], list[list[str]]]:
    def edit(pages: list[list[str]]) -> list[list[str]]:
        out = [[line.replace(old, new) if old in line else line for line in page] for page in pages]
        assert out != pages, f"edit {old!r} did not apply"
        return out

    return edit


def _replace_in(
    prefixes: tuple[str, ...], old: str, new: str
) -> Callable[[list[list[str]]], list[list[str]]]:
    """Replace `old` by `new` only on lines starting with one of `prefixes`."""

    def edit(pages: list[list[str]]) -> list[list[str]]:
        out = [
            [ln.replace(old, new) if ln.startswith(prefixes) else ln for ln in page]
            for page in pages
        ]
        assert out != pages, f"edit {old!r} did not apply"
        return out

    return edit


def _edit_line(prefix: str, new_line: str) -> Callable[[list[list[str]]], list[list[str]]]:
    def edit(pages: list[list[str]]) -> list[list[str]]:
        out = [[new_line if line.startswith(prefix) else line for line in page] for page in pages]
        assert out != pages
        return out

    return edit


# --------------------------------------------------------------------------------------------
# Protocol, detection
# --------------------------------------------------------------------------------------------


def test_parser_identity() -> None:
    assert isinstance(PARSER, TaxFormParser)
    assert PARSER.id == "tax_1040"
    assert PARSER.form_types == ("1040",)
    assert BUILDER.layout_id == PARSER.id
    assert SUPPORTED_1040_YEARS == (2022, 2023, 2024, 2025)
    assert set(F1040_FIELDS) == set(SUPPORTED_1040_YEARS)


@pytest.mark.parametrize("variant", VARIANTS)
def test_detect_fixtures(variant: str) -> None:
    assert PARSER.detect(extract_doc(generated_pdf(LAYOUT_ID, variant))) >= 0.9


@pytest.mark.parametrize(
    "heading",
    [
        "Form 1040-SR U.S. Tax Return for Seniors 2024",
        "Form 1040-NR U.S. Nonresident Alien Income Tax Return 2024",
        "Form 1040-X Amended U.S. Individual Income Tax Return 2024",
    ],
)
def test_unsupported_variants_score_zero_and_raise(heading: str) -> None:
    doc = _mem_doc(edit=_edit_line("Form 1040  U.S.", heading + "  Filing Status"))
    assert PARSER.detect(doc) == 0.0
    with pytest.raises(UnsupportedLayoutError) as err:
        PARSER.parse(doc)
    assert not _LONG_DIGITS.search(str(err.value))


def test_schedule_pages_alone_do_not_detect() -> None:
    doc = _mem_doc("self_employed", edit=lambda pages: pages[2:])
    assert PARSER.detect(doc) == 0.0


def test_unrelated_documents_score_zero() -> None:
    doc = doc_from_pages([["Wage and Tax Statement 2024", "Employer identification number"]])
    assert PARSER.detect(doc) == 0.0
    assert PARSER.detect(doc_from_pages([[]])) == 0.0


# --------------------------------------------------------------------------------------------
# Goldens
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", VARIANTS)
def test_matches_frozen_golden(variant: str) -> None:
    assert_matches_golden(_parse_fixture(variant), load_golden(LAYOUT_ID, variant))


@pytest.mark.parametrize("variant", VARIANTS)
def test_generator_output_equals_frozen_golden(variant: str) -> None:
    generated = json.loads(generated_pdf(LAYOUT_ID, variant).with_suffix(".json").read_text())
    assert generated == load_golden(LAYOUT_ID, variant)


# --------------------------------------------------------------------------------------------
# Explicit semantics
# --------------------------------------------------------------------------------------------


def test_normal_wages_only() -> None:
    form = _form("normal_wages_only")
    assert form.form_type == "1040" and form.tax_year == 2024
    assert form.filing_status == "single"
    assert form.agi_minor == 8_500_000
    assert form.taxable_income_minor == 8_500_000 - 1_460_000
    assert _income(form) == {"salary_wages": 8_500_000}
    amounts = _amounts(form)
    assert amounts["refund"] == 81_200 and "amount_owed" not in amounts
    assert amounts["total_payments"] - amounts["total_federal_tax"] == amounts["refund"]
    # all-ordinary: estimate puts everything in the ordinary bucket
    assert amounts["federal_tax_preferential_est"] == 0
    assert amounts["federal_tax_ordinary_est"] == amounts["total_federal_tax"]
    assert form.payer_name is None and form.payer_tin_last4 is None
    assert form.warnings == ()


def test_with_investment_income_split_and_memo() -> None:
    form = _form("with_investment_income")
    by_cat = {i.category: i for i in form.income}
    assert by_cat["qualified_dividends"].is_memo is True
    assert by_cat["ordinary_dividends"].is_memo is False
    assert by_cat["short_term_capital_gain"].amount_minor == 150_000
    assert by_cat["long_term_capital_gain"].amount_minor == 1_200_000
    assert all(i.source_form == "1040" for i in form.income)
    amounts = _amounts(form)
    assert amounts["net_investment_income_tax"] == 0  # printed as zero on Schedule 2
    est = [t for t in form.taxes if t.is_estimate]
    assert {t.category for t in est} == {"federal_tax_ordinary_est", "federal_tax_preferential_est"}
    assert amounts["federal_tax_preferential_est"] > 0
    # the split sums to the printed tax (Form 1040 line 16, no credits here)
    assert (
        amounts["federal_tax_ordinary_est"] + amounts["federal_tax_preferential_est"]
        == amounts["total_federal_tax"]
    )
    # independent re-computation of the worksheet from the printed figures
    assert form.taxable_income_minor == 9_495_000
    split = estimate.estimate_tax_split(
        tax_year=2024,
        filing_status="single",
        taxable_income_minor=9_495_000,
        qualified_dividends_minor=310_000,
        net_ltcg_minor=1_200_000,
        income_tax_minor=amounts["total_federal_tax"],
    )
    assert split is not None and not split.approximate
    assert amounts["federal_tax_preferential_est"] == split.preferential_minor
    assert amounts["federal_income_tax_withheld"] == 1_519_100


def test_high_income_niit_and_additional_medicare() -> None:
    form = _form("high_income_niit")
    amounts = _amounts(form)
    assert amounts["net_investment_income_tax"] == 353_400  # 3.8% of 93,000
    assert amounts["additional_medicare_tax"] == 99_000  # 0.9% of (310,000 - 200,000)
    assert amounts["amount_owed"] == 180_000 and "refund" not in amounts
    assert "self_employment_tax" not in amounts
    # NIIT is parsed, never estimated
    assert not any(t.is_estimate and "investment" in t.category for t in form.taxes)
    # line 24 includes the Schedule 2 taxes, the estimate split is of line 16 only
    split_total = amounts["federal_tax_ordinary_est"] + amounts["federal_tax_preferential_est"]
    assert amounts["total_federal_tax"] - split_total == 353_400 + 99_000


def test_self_employed() -> None:
    form = _form("self_employed")
    by_cat = {i.category: i.amount_minor for i in form.income}
    assert by_cat["self_employment"] == 6_200_000
    amounts = _amounts(form)
    assert amounts["self_employment_tax"] == 876_000
    assert amounts["estimated_tax_payments"] == 600_000
    assert form.agi_minor == 8_210_000 - 438_000  # half of the SE tax is adjusted out
    assert amounts["amount_owed"] == 215_000


def test_refund_2023_mfs() -> None:
    form = _form("refund")
    assert (form.tax_year, form.filing_status) == (2023, "mfs")
    assert _income(form)["salary_wages"] == 6_250_000
    assert _amounts(form)["refund"] == 315_000


def test_owed_2022_layout_hoh() -> None:
    form = _form("owed")
    assert (form.tax_year, form.filing_status) == (2022, "hoh")
    wages = next(i for i in form.income if i.category == "salary_wages")
    assert wages.box == "line 1"  # 2022 has no line 1a-1z
    assert wages.amount_minor == 5_800_000
    amounts = _amounts(form)
    assert amounts["amount_owed"] == 241_500 and "refund" not in amounts


def test_mfj_2025_layout() -> None:
    form = _form("mfj")
    assert (form.tax_year, form.filing_status) == (2025, "mfj")
    spec, calc = spec_for("mfj")
    assert form.agi_minor == calc.agi * 100  # line 11a
    assert form.taxable_income_minor == calc.taxable * 100
    amounts = _amounts(form)
    # child tax credit (line 19) lowers line 24 but not the estimate base (line 16)
    assert amounts["total_federal_tax"] == (calc.tax - spec.child_credit) * 100
    assert (
        amounts["federal_tax_ordinary_est"] + amounts["federal_tax_preferential_est"]
        == calc.tax * 100
    )
    assert amounts["refund"] == 50_000


def test_unsupported_year_warns_and_skips_estimate() -> None:
    parsed = _parse_fixture("unsupported_year")
    (form,) = parsed.forms
    assert form.tax_year == 2027
    assert "form layout for tax year 2027 not verified" in form.warnings
    assert not any(t.is_estimate for t in form.taxes)
    assert any("no federal bracket table" in w for w in form.warnings)
    # the latest layout is used as a fallback, so the lines are still read
    assert form.agi_minor == 7_200_000
    assert _income(form) == {"salary_wages": 7_200_000}


def test_year_override_beats_detection() -> None:
    doc = extract_doc(generated_pdf(LAYOUT_ID, "unsupported_year"))
    (form,) = PARSER.parse(doc, tax_year=2025).forms
    assert form.tax_year == 2025
    assert not any("not verified" in w for w in form.warnings)
    assert any(t.is_estimate for t in form.taxes)
    with pytest.raises(ParserError):
        PARSER.parse(doc, tax_year=1899)


def test_no_schedule_d_uses_line_7_as_long_term_qss() -> None:
    form = _form("no_schedule_d")
    assert form.filing_status == "qss"
    by_cat = {i.category: i for i in form.income}
    assert "short_term_capital_gain" not in by_cat
    assert by_cat["long_term_capital_gain"].amount_minor == 120_000
    assert by_cat["long_term_capital_gain"].box == "line 7"
    amounts = _amounts(form)
    # qss uses the mfj tables; 1,200 of gain + 600 qualified dividends sit in the 0% band
    assert amounts["federal_tax_preferential_est"] == 0
    assert form.warnings == ()


def test_refund_is_line_35a_not_the_overpayment() -> None:
    form = _form("refund_applied")
    amounts = _amounts(form)
    assert amounts["refund"] == 150_000  # 35a; line 34 overpaid 2,000 less 500 applied
    refund = next(t for t in form.taxes if t.category == "refund")
    assert refund.box == "line 35a"
    assert form.warnings == ()


def test_refund_falls_back_to_line_34_less_line_36() -> None:
    def edit(pages: list[list[str]]) -> list[list[str]]:
        return [[ln for ln in page if not ln.startswith("35a")] for page in pages]

    (form,) = PARSER.parse(_mem_doc("refund_applied", edit=edit)).forms
    refund = next(t for t in form.taxes if t.category == "refund")
    assert (refund.amount_minor, refund.box) == (150_000, "line 34")
    assert form.warnings == ()


def test_refund_equals_line_34_when_nothing_applied_and_no_35a() -> None:
    def edit(pages: list[list[str]]) -> list[list[str]]:
        return [[ln for ln in page if not ln.startswith("35a")] for page in pages]

    (form,) = PARSER.parse(_mem_doc("refund", edit=edit)).forms
    assert _amounts(form)["refund"] == 315_000


def test_whole_refund_applied_to_estimated_tax_is_no_refund() -> None:
    doc = _mem_doc(
        "refund_applied",
        edit=lambda pages: [
            [
                ln.replace("1,500.", "").replace("500.", "2,000.")
                if ln.startswith(("35a", "36"))
                else ln
                for ln in page
            ]
            for page in pages
        ],
    )
    (form,) = PARSER.parse(doc).forms
    assert "refund" not in _amounts(form)


def test_refund_consistency_warning_when_35a_and_36_do_not_add_up() -> None:
    doc = _mem_doc("refund_applied", edit=_replace_in(("36",), "500.", "300."))
    (form,) = PARSER.parse(doc).forms
    assert (
        "refund and amount applied to next year do not add up to the overpayment" in form.warnings
    )


def test_missing_filing_status() -> None:
    parsed = _parse_fixture("missing_filing_status")
    (form,) = parsed.forms
    assert form.filing_status is None
    assert form.agi_minor == 6_800_000
    assert not any(t.is_estimate for t in form.taxes)
    assert form.warnings == ("filing status not found; federal tax split not estimated",)


@pytest.mark.parametrize("variant", VARIANTS)
def test_signs_and_units(variant: str) -> None:
    form = _form(variant)
    assert all(t.amount_minor >= 0 for t in form.taxes)
    negative = [i for i in form.income if i.amount_minor < 0]
    assert all("capital_gain" in i.category for i in negative)
    assert all(t.source_form == "1040" for t in form.taxes)
    assert all(i.source_form == "1040" for i in form.income)
    _, calc = spec_for(variant)
    amounts = _amounts(form)
    assert amounts["total_federal_tax"] == calc.total_tax * 100
    assert amounts["total_payments"] == calc.total_payments * 100
    assert form.agi_minor == calc.agi * 100


@pytest.mark.parametrize("variant", VARIANTS)
def test_printed_tax_equals_worksheet(variant: str) -> None:
    """The fixtures are meaningful for the estimate: line 16 is the worksheet result."""
    spec, calc = spec_for(variant)
    year = spec.calc_year or spec.year
    status = spec.status or "single"
    assert (
        estimate.worksheet_tax(
            tax_year=year,
            filing_status=status,
            taxable_income_minor=calc.taxable * 100,
            qualified_dividends_minor=calc.qualified_dividends * 100,
            net_ltcg_minor=calc.net_ltcg * 100,
        )
        == calc.tax * 100
    )
    assert calc.total_payments - calc.total_tax == calc.refund - calc.owed


# --------------------------------------------------------------------------------------------
# Filing status detection styles
# --------------------------------------------------------------------------------------------

_STATUS_LABELS = {
    "single": "Single",
    "mfj": "Married filing jointly (even if only one had income)",
    "mfs": "Married filing separately (MFS)",
    "hoh": "Head of household (HOH)",
    "qss": "Qualifying surviving spouse (QSS)",
}


def _status_edit(rows: list[str]) -> Callable[[list[list[str]]], list[list[str]]]:
    def edit(pages: list[list[str]]) -> list[list[str]]:
        first = pages[0]
        start = next(i for i, line in enumerate(first) if line.startswith("Filing Status"))
        new_first = first[:start] + rows + first[start + 5 :]
        return [new_first, *pages[1:]]

    return edit


def _status_rows(checked: str | None, style: str) -> list[str]:
    rows: list[str] = []
    for i, (key, label) in enumerate(_STATUS_LABELS.items()):
        on = key == checked
        prefix = "Filing Status  " if i == 0 else ""
        if style == "bracket":
            rows.append(f"{prefix}{'[X]' if on else '[ ]'} {label}")
        elif style == "paren":
            rows.append(f"{prefix}{'(X)' if on else '( )'} {label}")
        elif style == "bare":
            rows.append(f"{prefix}{'X ' if on else ''}{label}")
        elif style == "glyph":
            rows.append(f"{prefix}{'☒' if on else '☐'} {label}")
        elif style == "after":
            rows.append(f"{prefix}{label} {'[X]' if on else '[ ]'}")
        else:  # pragma: no cover
            raise AssertionError(style)
    return rows


@pytest.mark.parametrize("style", ["bracket", "paren", "bare", "glyph", "after"])
@pytest.mark.parametrize("status", list(_STATUS_LABELS))
def test_filing_status_styles(status: str, style: str) -> None:
    doc = _mem_doc(edit=_status_edit(_status_rows(status, style)))
    (form,) = PARSER.parse(doc).forms
    assert form.filing_status == status


def test_filing_status_none_marked_or_several() -> None:
    (none_form,) = PARSER.parse(_mem_doc(edit=_status_edit(_status_rows(None, "bracket")))).forms
    assert none_form.filing_status is None
    assert none_form.warnings == ("filing status not found; federal tax split not estimated",)
    rows = _status_rows("single", "bracket")
    rows[1] = rows[1].replace("[ ]", "[X]")
    (many,) = PARSER.parse(_mem_doc(edit=_status_edit(rows))).forms
    assert many.filing_status is None
    assert many.warnings == ("several filing statuses are marked; federal tax split not estimated",)
    assert not any(t.is_estimate for t in many.taxes)


# --------------------------------------------------------------------------------------------
# Layout quirks
# --------------------------------------------------------------------------------------------


def test_blank_cell_does_not_steal_neighbouring_column() -> None:
    """``3a Qualified dividends`` is blank but ``b Ordinary dividends`` has a value: the value
    belongs to 3b only."""
    spec = _Spec(2024, "single", wages=50_000, ordinary_dividends=2_000)
    doc = doc_from_pages(_pages(spec, _calc(spec)))
    (form,) = PARSER.parse(doc).forms
    assert _income(form) == {"salary_wages": 5_000_000, "ordinary_dividends": 200_000}
    assert not any(i.category == "qualified_dividends" for i in form.income)


def test_blank_line_with_numeric_line_reference_is_not_an_amount() -> None:
    """Lines 34/37 print their own number at the right when empty (``... 37``)."""
    assert "amount_owed" not in _amounts(_form("normal_wages_only"))
    assert "refund" not in _amounts(_form("owed"))


def test_negative_capital_gain_is_signed_and_loss_limit_noted_in_line_7() -> None:
    spec = _Spec(2024, "single", wages=60_000, short_term=-4_000, long_term=-2_000, schedule_d=True)
    doc = doc_from_pages(_pages(spec, _calc(spec)))
    (form,) = PARSER.parse(doc).forms
    by_cat = {i.category: i.amount_minor for i in form.income}
    assert by_cat["short_term_capital_gain"] == -400_000
    assert by_cat["long_term_capital_gain"] == -200_000
    # 1040 line 7 shows the limited loss (3,000); total income is consistent with it
    assert form.warnings == ()
    assert form.agi_minor == 5_700_000


def test_line_number_fallback_when_label_is_reworded() -> None:
    doc = _mem_doc(
        edit=_replace(
            "Subtract line 10 from line 9. This is your adjusted gross income",
            "Subtract line 10 from line 9. Your AGI",
        )
    )
    (form,) = PARSER.parse(doc).forms
    assert form.agi_minor == 8_500_000


def test_withholding_falls_back_to_lines_25a_to_25c() -> None:
    def edit(pages: list[list[str]]) -> list[list[str]]:
        return [[ln for ln in page if not ln.startswith("25d")] for page in pages]

    (form,) = PARSER.parse(_mem_doc("with_investment_income", edit=edit)).forms
    assert _amounts(form)["federal_income_tax_withheld"] == 1_519_100


# --------------------------------------------------------------------------------------------
# Consistency warnings and errors
# --------------------------------------------------------------------------------------------


def test_warning_refund_and_owed_both_printed() -> None:
    def edit(pages: list[list[str]]) -> list[list[str]]:
        return [
            [ln.rstrip() + " 500." if ln.startswith("37 ") else ln for ln in page] for page in pages
        ]

    (form,) = PARSER.parse(_mem_doc("normal_wages_only", edit=edit)).forms
    assert "both a refund and an amount owed are printed" in form.warnings
    assert "refund or amount owed does not match total payments minus total tax" in form.warnings


def test_warning_refund_does_not_match_payments() -> None:
    doc = _mem_doc("refund", edit=_replace("3,150.", "2,000."))
    (form,) = PARSER.parse(doc).forms
    assert "refund or amount owed does not match total payments minus total tax" in form.warnings


def test_warning_income_lines_do_not_add_up() -> None:
    doc = _mem_doc("normal_wages_only", edit=_replace_in(("1a", "1z"), "85,000.", "84,000."))
    (form,) = PARSER.parse(doc).forms
    assert "sum of income lines does not match the printed total income" in form.warnings


def test_warning_split_is_approximate_when_printed_tax_differs() -> None:
    _, calc = spec_for("with_investment_income")
    doc = _mem_doc(
        "with_investment_income", edit=_replace(f"{calc.tax:,}.", f"{calc.tax + 900:,}.")
    )
    (form,) = PARSER.parse(doc).forms
    assert "federal tax split is approximate" in form.warnings
    amounts = _amounts(form)
    assert (
        amounts["federal_tax_ordinary_est"] + amounts["federal_tax_preferential_est"]
        == (calc.tax + 900) * 100
    )


def test_warning_schedule_d_special_rate_gains() -> None:
    doc = _mem_doc("with_investment_income", edit=_replace_in(("18 ",), " 0.", " 2,000."))
    (form,) = PARSER.parse(doc).forms
    assert "federal tax split is approximate" in form.warnings


def test_business_loss_is_not_an_income_item() -> None:
    doc = _mem_doc("self_employed", edit=_replace_in(("3 ", "31 "), "62,000.", "(5,000.)"))
    (form,) = PARSER.parse(doc).forms
    assert "business loss reported; it is not included in income items" in form.warnings
    assert not any(i.category == "self_employment" for i in form.income)


def test_key_lines_missing_is_a_generic_error() -> None:
    doc = doc_from_pages(
        [
            [
                "Form 1040  U.S. Individual Income Tax Return  2024",
                "Filing Status  [X] Single",
                "1a Total amount from Form(s) W-2, box 1 . . . 1a 85,000.",
            ]
        ]
    )
    with pytest.raises(ParserError) as err:
        PARSER.parse(doc)
    assert not _LONG_DIGITS.search(str(err.value))


def test_missing_year_is_an_error_unless_overridden() -> None:
    lines = [
        "Form 1040  U.S. Individual Income Tax Return",
        "Filing Status  [X] Single",
        "11 Subtract line 10 from line 9. This is your adjusted gross income . . . 11 85,000.",
        "15 Subtract line 14 from line 11. If zero or less, enter -0- . . . 15 70,400.",
        "16 Tax (see instructions) . . . 16 11,000.",
        "24 Add lines 22 and 23. This is your total tax . . . 24 11,000.",
    ]
    doc = doc_from_pages([lines])
    with pytest.raises(ParserError):
        PARSER.parse(doc)
    (form,) = PARSER.parse(doc, tax_year=2024).forms
    assert form.tax_year == 2024 and form.agi_minor == 8_500_000
    assert form.taxable_income_minor == 7_040_000
    assert _amounts(form)["total_federal_tax"] == 1_100_000


# --------------------------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", VARIANTS)
def test_nothing_identifying_is_captured(variant: str) -> None:
    parsed = _parse_fixture(variant)
    blob = repr(parsed) + json.dumps(asdict(parsed))
    for needle in ("912-34-5678", "923-45-6789", "Testperson", "Jordan", "Riley", "Example Lane"):
        assert needle not in blob
    assert not re.search(r"\d{3}-\d{2}-\d{4}", blob)
    assert not re.search(r"(?<![\d.,])\d{9}(?![\d.,])", blob)
    for form in parsed.forms:
        assert form.payer_name is None
        assert form.payer_tin_last4 is None and form.account_last4 is None
        for warning in form.warnings:
            assert not _LONG_DIGITS.search(warning)


def test_goldens_are_for_1040_forms() -> None:
    for variant in VARIANTS:
        golden = load_golden(LAYOUT_ID, variant)
        assert golden["tax_year"] == golden["forms"][0]["tax_year"]
        assert golden["forms"][0]["form_type"] == "1040"


# --------------------------------------------------------------------------------------------
# Inline form references, page headings, shared status rows
# --------------------------------------------------------------------------------------------

_LINE16_CHECKBOX_STYLES = [
    "16 Tax (see instructions). Check if any from Form(s): 1 [ ] 8814 2 [ ] 4972 3 [ ] . . . 16 {}",
    "16 Tax (see instructions). Check if any from Form(s): 1 \u2610 8814 2 \u2610 4972 3 \u2610  .  .  16 {}",
    "16 Tax (see instructions). Check if any from Form(s): 1 \u2610 8814 2 \u2610 4972 3 \u2610 . . .",
]


@pytest.mark.parametrize("template", _LINE16_CHECKBOX_STYLES[:2])
def test_line_16_ignores_inline_form_numbers(template: str) -> None:
    """Real rows read ``16 Tax ... Check if any from Form(s): 1 [ ] 8814 2 [ ] 4972 ...  16  11,000.``"""
    _, calc = spec_for("normal_wages_only")
    new = template.format(f"{calc.tax:,}.")
    form = PARSER.parse(_mem_doc(edit=_edit_line("16 ", new))).forms[0]
    split = {t.category: t.amount_minor for t in form.taxes if t.is_estimate}
    assert sum(split.values()) == calc.tax * 100
    assert form.warnings == ()


def test_line_16_blank_with_inline_form_numbers_is_not_an_amount() -> None:
    form = PARSER.parse(
        _mem_doc(
            edit=_edit_line(
                "16 ", _LINE16_CHECKBOX_STYLES[2].replace("3 \u2610 . . .", "3 \u2610 . . . 16")
            )
        )
    ).forms[0]
    assert not any(t.is_estimate for t in form.taxes)
    assert "taxable income or tax not read; federal tax split not estimated" in form.warnings


def test_withholding_rows_in_irs_wording() -> None:
    """The printed form reads ``25 Federal income tax withheld from: a Form(s) W-2 ... 25a``."""

    def edit(pages: list[list[str]]) -> list[list[str]]:
        out = []
        for page in pages:
            new = []
            for ln in page:
                if ln.startswith("25a"):
                    ln = ln.replace("25a   ", "25 Federal income tax withheld from: a ", 1)
                    ln = ln.replace(
                        "25a Form(s) W-2", "25 Federal income tax withheld from: a Form(s) W-2", 1
                    )
                elif ln.startswith("25b"):
                    ln = ln.replace("25b ", "b ", 1)
                elif ln.startswith("25d"):
                    continue
                new.append(ln)
            out.append(new)
        return out

    (form,) = PARSER.parse(_mem_doc("with_investment_income", edit=edit)).forms
    assert _amounts(form)["federal_income_tax_withheld"] == 1_519_100


def test_schedule_1a_page_is_not_schedule_1() -> None:
    spec = _Spec(2025, "single", wages=60_000)
    pages = _pages(spec, _calc(spec))
    pages.append(
        [
            "SCHEDULE 1-A (Form 1040)  Additional Deductions  2025",
            "3 Business income or (loss). Attach Schedule C . . . . . . . . . 3  9,999.",
        ]
    )
    (form,) = PARSER.parse(doc_from_pages(pages)).forms
    assert "self_employment" not in _income(form)
    assert _income(form)["salary_wages"] == 6_000_000


_SHARED_ROW = (
    "Filing Status [ ] Single [ ] Married filing jointly [ ] Married filing separately "
    "[ ] Head of household [ ] Qualifying surviving spouse"
)


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("single", "Single"),
        ("mfj", "Married filing jointly"),
        ("mfs", "Married filing separately"),
        ("hoh", "Head of household"),
        ("qss", "Qualifying surviving spouse"),
    ],
)
@pytest.mark.parametrize("style", ["before", "after"])
def test_filing_status_with_all_options_on_one_row(status: str, label: str, style: str) -> None:
    row = _SHARED_ROW
    if style == "before":
        row = row.replace(f"[ ] {label}", f"[X] {label}")
    else:
        row = row.replace("[ ] ", "")
        parts = [
            "Single",
            "Married filing jointly",
            "Married filing separately",
            "Head of household",
            "Qualifying surviving spouse",
        ]
        row = "Filing Status " + " ".join(f"{p} {'[X]' if p == label else '[ ]'}" for p in parts)
    doc = _mem_doc(edit=_status_edit([row]))
    (form,) = PARSER.parse(doc).forms
    assert form.filing_status == status


def test_shared_status_row_none_or_several_marked() -> None:
    (none_form,) = PARSER.parse(_mem_doc(edit=_status_edit([_SHARED_ROW]))).forms
    assert none_form.filing_status is None
    assert none_form.warnings[0].startswith("filing status not found")
    two = _SHARED_ROW.replace("[ ] Single", "[X] Single").replace("[ ] Head", "[X] Head")
    (many,) = PARSER.parse(_mem_doc(edit=_status_edit([two]))).forms
    assert many.filing_status is None
    assert many.warnings[0].startswith("several filing statuses are marked")


def test_shared_status_row_with_glyph_boxes() -> None:
    row = _SHARED_ROW.replace("[ ]", "\u2610").replace(
        "\u2610 Married filing separately", "\u2612 Married filing separately"
    )
    (form,) = PARSER.parse(_mem_doc(edit=_status_edit([row]))).forms
    assert form.filing_status == "mfs"
