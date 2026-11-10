"""Synthetic fixture generator for a consolidated broker 1099 (`tax_1099_consolidated`).

Software-style layout (label ... value on one row): a header, a ``Form 1099 Summary`` block, then
per account an ``Account number`` row followed by ``1099-INT`` / ``1099-DIV`` / ``1099-B`` /
``1099-MISC`` sections (headings printed as ``Form 1099-DIV ...`` like real broker documents; the
single-form parsers halve their scores because other headings / the consolidated marker appear). The 1099-B section reuses `tax_1099_b.section_lines`. Each account yields one
``1099-consolidated`` form in the golden.

Variants: ``int_div_b``, ``int_div_b_misc`` (adds MISC and a state withholding row),
``div_only`` (shell with a single section), ``multi_account`` (two accounts, different sections).

Identity / wording variants (dedupe regressions): ``alt_labels`` (``Payer:`` / ``Payer EIN:`` /
``Account No.`` wording and ``Box n`` / ``1a Total ...`` amount labels), ``two_payers_one_file``
(two payers' statements in one file), ``noid_a`` / ``noid_b`` (two different payers, identical
amounts, no payer TIN and an account line the parser does not know), ``noid_anon`` (as ``noid_a``
but no payer line at all) and ``no_amounts`` (sections whose amounts use unknown wording: no form
is emitted, parse error).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from personal_finance_analyzer.core.tax_types import (
    IncomeCategory,
    ParsedIncomeItem,
    ParsedTaxDocument,
    ParsedTaxForm,
    ParsedTaxItem,
)
from tests.fixtures.gen.base import format_row, render_lines_pdf
from tests.fixtures.gen.tax_1099_b import (
    B_TITLE,
    TAX_YEAR,
    Lot,
    expected_capital_items,
    expected_withholding,
    fmt,
    section_lines,
)
from tests.fixtures.gen.tax_common import (
    ACCOUNT_NUMBER,
    BROKER,
    RECIPIENT_NAME,
    RECIPIENT_SSN,
    build_golden,
    last4,
    money,
)

SECOND_ACCOUNT_NUMBER = "SYN-000987654321"
_WIDTHS = (58, 14)


@dataclass(frozen=True, slots=True)
class _Section:
    lines: list[str]
    income: list[ParsedIncomeItem]
    taxes: list[ParsedTaxItem]


def _row(label: str, cents: int) -> str:
    return format_row([label, fmt(cents)], _WIDTHS)


def _fed(form: str, cents: int) -> list[ParsedTaxItem]:
    if cents <= 0:
        return []
    return [
        ParsedTaxItem(
            "federal_income_tax_withheld", cents, form, "4", "Federal income tax withheld"
        )
    ]


def _state(form: str, state: tuple[str, int] | None) -> tuple[list[str], list[ParsedTaxItem]]:
    if state is None:
        return [], []
    code, cents = state
    return (
        [f"State {code} State income tax withheld (Box 16)   {fmt(cents)}"],
        [
            ParsedTaxItem(
                "state_income_tax_withheld",
                cents,
                form,
                "16",
                "State income tax withheld",
                jurisdiction=code,
            )
        ],
    )


def _item(
    category: IncomeCategory, cents: int, form: str, box: str, label: str, memo: bool = False
) -> ParsedIncomeItem:
    return ParsedIncomeItem(category, cents, form, box, label, is_memo=memo)


def int_section(interest: str, bonds: str, exempt: str, withheld: str) -> _Section:
    f = "1099-INT"
    i, b, e, w = money(interest), money(bonds), money(exempt), money(withheld)
    lines = [
        "Form 1099-INT  Interest Income",
        _row("Interest income (Box 1)", i),
        _row("Interest on U.S. Savings Bonds (Box 3)", b),
        _row("Tax-exempt interest (Box 8)", e),
        _row("Federal income tax withheld (Box 4)", w),
        "",
    ]
    income = [
        _item("interest", i, f, "1", "Interest income"),
        _item("interest", b, f, "3", "Interest on U.S. savings bonds and Treasury obligations"),
        _item("other", e, f, "8", "Tax-exempt interest", True),
    ]
    return _Section(lines, income, _fed(f, w))


def div_section(
    ordinary: str,
    qualified: str,
    capgain: str,
    withheld: str,
    state: tuple[str, int] | None = None,
) -> _Section:
    f = "1099-DIV"
    o, q, g, w = money(ordinary), money(qualified), money(capgain), money(withheld)
    state_lines, state_taxes = _state(f, state)
    lines = [
        "Form 1099-DIV  Dividends and Distributions",
        _row("Ordinary dividends (Box 1a)", o),
        _row("Qualified dividends (Box 1b)", q),
        _row("Capital gain distributions (Box 2a)", g),
        _row("Federal income tax withheld (Box 4)", w),
        *state_lines,
        "",
    ]
    income = [
        _item("ordinary_dividends", o, f, "1a", "Ordinary dividends"),
        _item("qualified_dividends", q, f, "1b", "Qualified dividends", True),
        _item("long_term_capital_gain", g, f, "2a", "Capital gain distributions"),
    ]
    return _Section(lines, income, [*_fed(f, w), *state_taxes])


def misc_section(rents: str, royalties: str, other: str, withheld: str) -> _Section:
    f = "1099-MISC"
    r, y, o, w = money(rents), money(royalties), money(other), money(withheld)
    lines = [
        "Form 1099-MISC  Miscellaneous Information",
        _row("Rents (Box 1)", r),
        _row("Royalties (Box 2)", y),
        _row("Other income (Box 3)", o),
        _row("Federal income tax withheld (Box 4)", w),
        "",
    ]
    income = [
        _item("other", r, f, "1", "Rents"),
        _item("other", y, f, "2", "Royalties"),
        _item("other", o, f, "3", "Other income"),
    ]
    return _Section(lines, income, _fed(f, w))


def b_section(lots: Sequence[Lot], *, summary: bool, detail: bool, withheld: str) -> _Section:
    w = money(withheld)
    lines = section_lines(
        lots, heading=f"Form 1099-B  {B_TITLE}", summary=summary, detail=detail, withheld=w
    )
    return _Section(
        [*lines, ""],
        expected_capital_items(lots, summary=summary),
        expected_withholding(w, None),
    )


def _lot(term: str, covered: bool, desc: str, acq: str, sold: str, proceeds: str, cost: str) -> Lot:
    return Lot(term, covered, desc, acq, sold, money(proceeds), money(cost))


_B_LOTS = (
    _lot("short", True, "100 sh QRS CORP", "01/15/2024", "03/20/2024", "5,250.00", "4,800.00"),
    _lot("long", True, "250 sh WXY CO", "05/04/2022", "08/19/2024", "12,400.00", "8,100.00"),
)
_B_LOSS_LOTS = (
    _lot("short", True, "300 sh DEF CORP", "02/12/2024", "04/01/2024", "3,000.00", "5,500.00"),
    _lot("long", False, "75 sh LMN FUND", "11/30/2021", "09/09/2024", "2,000.00", "2,600.00"),
)


def _accounts(variant: str) -> list[tuple[str, list[_Section]]]:
    interest = int_section("1,234.56", "210.00", "80.00", "0.00")
    dividends = div_section("2,450.75", "1,900.25", "310.40", "25.00")
    if variant == "int_div_b":
        return [
            (
                ACCOUNT_NUMBER,
                [
                    interest,
                    dividends,
                    b_section(_B_LOTS, summary=True, detail=False, withheld="0.00"),
                ],
            )
        ]
    if variant == "int_div_b_misc":
        return [
            (
                ACCOUNT_NUMBER,
                [
                    interest,
                    div_section("2,450.75", "1,900.25", "310.40", "25.00", ("CA", money("12.00"))),
                    b_section(_B_LOTS, summary=True, detail=True, withheld="0.00"),
                    misc_section("1,200.00", "340.00", "75.50", "10.00"),
                ],
            )
        ]
    if variant == "div_only":
        return [(ACCOUNT_NUMBER, [dividends])]
    if variant == "multi_account":
        return [
            (ACCOUNT_NUMBER, [dividends, int_section("640.10", "0.00", "0.00", "0.00")]),
            (
                SECOND_ACCOUNT_NUMBER,
                [
                    b_section(_B_LOSS_LOTS, summary=True, detail=False, withheld="40.00"),
                    div_section("118.00", "90.00", "0.00", "0.00"),
                ],
            ),
        ]
    raise ValueError(variant)


def _summary_lines(accounts: list[tuple[str, list[_Section]]]) -> list[str]:
    sections = [s for _, secs in accounts for s in secs]
    totals: dict[str, int] = {"interest": 0, "dividends": 0, "gain": 0, "withheld": 0}
    for s in sections:
        for item in s.income:
            if item.category == "interest":
                totals["interest"] += item.amount_minor
            elif item.category == "ordinary_dividends":
                totals["dividends"] += item.amount_minor
            elif item.category in ("short_term_capital_gain", "long_term_capital_gain"):
                totals["gain"] += item.amount_minor
        for tax in s.taxes:
            if tax.category == "federal_income_tax_withheld":
                totals["withheld"] += tax.amount_minor
    return [
        "Form 1099 Summary",
        _row("Total interest income", totals["interest"]),
        _row("Total ordinary dividends", totals["dividends"]),
        _row("Net capital gain or loss", totals["gain"]),
        _row("Total federal income tax withheld", totals["withheld"]),
        "",
    ]


def _alt_int(interest: str) -> _Section:
    f, i = "1099-INT", money(interest)
    lines = ["1099-INT  Interest Income", _row("Box 1 Interest income", i), ""]
    return _Section(lines, [_item("interest", i, f, "1", "Interest income")], [])


def _alt_div(ordinary: str, capgain: str) -> _Section:
    f, o, g = "1099-DIV", money(ordinary), money(capgain)
    lines = [
        "1099-DIV  Dividends and Distributions",
        _row("1a Total ordinary dividends", o),
        _row("2a Total capital gain distributions", g),
        "",
    ]
    income = [
        _item("ordinary_dividends", o, f, "1a", "Ordinary dividends"),
        _item("long_term_capital_gain", g, f, "2a", "Capital gain distributions"),
    ]
    return _Section(lines, income, [])


_TWO_PAYERS = (
    ("Alder Brokerage Group", "99-0000031", "SYN-0000002222", "1,100.00", "210.00"),
    ("Birchwood Securities", "99-0000032", "SYN-0000004444", "640.25", "75.00"),
)


def _form(
    payer: str | None,
    tin: str | None,
    account: str | None,
    sections: Sequence[_Section],
) -> ParsedTaxForm:
    return ParsedTaxForm(
        form_type="1099-consolidated",
        tax_year=TAX_YEAR,
        payer_name=payer,
        payer_tin_last4=last4(tin) if tin else None,
        account_last4=last4(account) if account else None,
        is_corrected=False,
        filing_status=None,
        agi_minor=None,
        taxable_income_minor=None,
        income=tuple(i for sec in sections for i in sec.income),
        taxes=tuple(t for sec in sections for t in sec.taxes),
    )


def _build_identity_variant(out_dir: Path, name: str, variant: str) -> dict[str, Any]:
    """The wording / identity variants (see the module docstring)."""
    header = ["Consolidated Form 1099 Tax Information", f"Tax Year {TAX_YEAR}"]
    lines: list[str]
    forms: list[ParsedTaxForm]
    if variant == "alt_labels":
        secs = [_alt_int("1,234.56"), _alt_div("2,450.75", "310.40")]
        lines = [
            *header,
            f"Payer: {BROKER.name}",
            f"Payer EIN: {BROKER.ein}",
            f"Recipient's name: {RECIPIENT_NAME}",
            f"Recipient's TIN: {RECIPIENT_SSN}",
            "",
            f"Account No. {ACCOUNT_NUMBER}",
            "",
            *[ln for sec in secs for ln in sec.lines],
        ]
        forms = [_form(BROKER.name, BROKER.ein, ACCOUNT_NUMBER, secs)]
    elif variant == "two_payers_one_file":
        lines = list(header)
        forms = []
        for payer, ein, account, interest, ordinary in _TWO_PAYERS:
            secs = [_alt_int(interest), _alt_div(ordinary, "0.00")]
            lines += [
                f"Payer's name: {payer}",
                f"Payer's TIN: {ein}",
                f"Account number: {account}",
                "",
                *[ln for sec in secs for ln in sec.lines],
            ]
            forms.append(_form(payer, ein, account, secs))
    elif variant in ("noid_a", "noid_b", "noid_anon"):
        secs = [_alt_int("500.00"), _alt_div("120.00", "0.00")]
        payer = {"noid_a": "Alder Brokerage Group", "noid_b": "Birchwood Securities"}.get(variant)
        lines = [*header]
        if payer:
            lines.append(f"Payer: {payer}")
        lines += ["Acct ref: QX-77", "", *[ln for sec in secs for ln in sec.lines]]
        forms = [_form(payer, None, None, secs)]
    elif variant == "no_amounts":
        lines = [
            *header,
            f"Payer: {BROKER.name}",
            f"Account No. {ACCOUNT_NUMBER}",
            "",
            "1099-INT  Interest Income",
            "Interest paid to you during the year        10.00",
            "1099-DIV  Dividends and Distributions",
            "Dividends paid to you during the year       20.00",
        ]
        render_lines_pdf(out_dir / f"{name}_{variant}.pdf", [lines])
        return {"expect_error": True}
    else:
        raise ValueError(variant)
    render_lines_pdf(out_dir / f"{name}_{variant}.pdf", [lines])
    doc = ParsedTaxDocument(tax_year=TAX_YEAR, forms=tuple(forms))
    return build_golden(doc)


class Tax1099ConsolidatedBuilder:
    layout_id = "tax_1099_consolidated"
    variants: tuple[str, ...] = (
        "int_div_b",
        "int_div_b_misc",
        "div_only",
        "multi_account",
        "alt_labels",
        "two_payers_one_file",
        "noid_a",
        "noid_b",
        "noid_anon",
        "no_amounts",
    )

    def build(self, out_dir: Path, *, variant: str, seed: int) -> dict[str, Any]:
        if variant in self.variants[4:]:
            return _build_identity_variant(out_dir, self.layout_id, variant)
        accounts = _accounts(variant)
        lines = [
            BROKER.name,
            "Consolidated Form 1099 Tax Information",
            f"Tax Year {TAX_YEAR}",
            f"Payer's name: {BROKER.name}",
            f"Payer's TIN: {BROKER.ein}",
            f"Recipient's name: {RECIPIENT_NAME}",
            f"Recipient's TIN: {RECIPIENT_SSN}",
            "",
            *_summary_lines(accounts),
        ]
        forms: list[ParsedTaxForm] = []
        for number, sections in accounts:
            lines += [f"Account number: {number}", ""]
            for section in sections:
                lines += section.lines
            forms.append(
                ParsedTaxForm(
                    form_type="1099-consolidated",
                    tax_year=TAX_YEAR,
                    payer_name=BROKER.name,
                    payer_tin_last4=last4(BROKER.ein),
                    account_last4=last4(number),
                    is_corrected=False,
                    filing_status=None,
                    agi_minor=None,
                    taxable_income_minor=None,
                    income=tuple(i for s in sections for i in s.income),
                    taxes=tuple(t for s in sections for t in s.taxes),
                )
            )
        page_size = 48
        pages = [lines[i : i + page_size] for i in range(0, len(lines), page_size)]
        render_lines_pdf(out_dir / f"{self.layout_id}_{variant}.pdf", pages)
        doc = ParsedTaxDocument(tax_year=TAX_YEAR, forms=tuple(forms))
        return build_golden(doc)


BUILDER = Tax1099ConsolidatedBuilder()
