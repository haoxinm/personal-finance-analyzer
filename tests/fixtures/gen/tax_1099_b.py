"""Synthetic fixture generator for Form 1099-B (`tax_1099_b`).

Software-style layout (label ... value on one row, `render_lines_pdf`): a summary table by term and
category, transaction detail rows under term headings, box 4 federal withholding and an optional
state withholding row. `section_lines` and `expected_capital_items` are reused by the consolidated
1099 generator for its 1099-B section.

Variants: ``normal_summary`` (summary rows + withholding), ``transactions_only`` (detail rows
only, summed), ``loss_year`` (negative short-term net), ``wash_sale`` (adjustments reconcile,
summary and detail agree), ``mismatch`` (printed short-term net disagrees with proceeds - cost +
adjustment: warning).
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
from tests.fixtures.gen.tax_common import (
    ACCOUNT_NUMBER,
    BROKER,
    RECIPIENT_NAME,
    RECIPIENT_SSN,
    build_golden,
    last4,
    money,
)

TAX_YEAR = 2024
B_TITLE = "Proceeds From Broker and Barter Exchange Transactions"

_SUMMARY_WIDTHS = (26, 12, 12, 12, 12)
_DETAIL_WIDTHS = (18, 11, 11, 11, 11, 9, 11)

#: (term, covered) -> (summary row label, detail heading)
_CATEGORIES: dict[tuple[str, bool], tuple[str, str]] = {
    ("short", True): ("Short-term (covered)", "Short-term transactions (Box A, covered)"),
    ("short", False): ("Short-term (noncovered)", "Short-term transactions (Box B, noncovered)"),
    ("long", True): ("Long-term (covered)", "Long-term transactions (Box D, covered)"),
    ("long", False): ("Long-term (noncovered)", "Long-term transactions (Box E, noncovered)"),
}


@dataclass(frozen=True, slots=True)
class Lot:
    """One sale. Amounts in cents; ``gain = proceeds - cost + wash``."""

    term: str  # "short" | "long"
    covered: bool
    description: str
    acquired: str
    sold: str
    proceeds: int
    cost: int
    wash: int = 0

    @property
    def gain(self) -> int:
        return self.proceeds - self.cost + self.wash


def fmt(cents: int) -> str:
    """Printed amount: ``-1,234.56`` for negatives."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100:,}.{cents % 100:02d}"


def _groups(lots: Sequence[Lot]) -> dict[tuple[str, bool], list[Lot]]:
    grouped: dict[tuple[str, bool], list[Lot]] = {}
    for key in _CATEGORIES:
        members = [lot for lot in lots if (lot.term, lot.covered) == key]
        if members:
            grouped[key] = members
    return grouped


def section_lines(
    lots: Sequence[Lot],
    *,
    heading: str,
    summary: bool,
    detail: bool,
    printed_net: dict[tuple[str, bool], int] | None = None,
    withheld: int = 0,
    state_withheld: tuple[str, int] | None = None,
) -> list[str]:
    """Lines of one 1099-B block (no identity lines): heading, optional summary table, optional
    detail tables, withholding rows. `printed_net` overrides the printed net per category."""
    printed_net = printed_net or {}
    groups = _groups(lots)
    lines = [heading, ""]
    if summary:
        lines.append("Summary of Sales by Term")
        lines.append(
            format_row(
                ["Term", "Proceeds", "Cost basis", "Wash adj", "Net gain/loss"], _SUMMARY_WIDTHS
            )
        )
        for key, members in groups.items():
            net = printed_net.get(key, sum(m.gain for m in members))
            lines.append(
                format_row(
                    [
                        _CATEGORIES[key][0],
                        fmt(sum(m.proceeds for m in members)),
                        fmt(sum(m.cost for m in members)),
                        fmt(sum(m.wash for m in members)),
                        fmt(net),
                    ],
                    _SUMMARY_WIDTHS,
                )
            )
        lines.append("")
    if detail:
        for key, members in groups.items():
            lines.append(_CATEGORIES[key][1])
            lines.append(
                format_row(
                    [
                        "Description",
                        "Acquired",
                        "Sold",
                        "Proceeds",
                        "Cost basis",
                        "Wash",
                        "Gain/Loss",
                    ],
                    _DETAIL_WIDTHS,
                )
            )
            for m in members:
                lines.append(
                    format_row(
                        [
                            m.description,
                            m.acquired,
                            m.sold,
                            fmt(m.proceeds),
                            fmt(m.cost),
                            fmt(m.wash),
                            fmt(m.gain),
                        ],
                        _DETAIL_WIDTHS,
                    )
                )
            lines.append(
                format_row(
                    [
                        f"Total {_CATEGORIES[key][0].lower()}",
                        "",
                        "",
                        fmt(sum(m.proceeds for m in members)),
                        fmt(sum(m.cost for m in members)),
                        fmt(sum(m.wash for m in members)),
                        fmt(sum(m.gain for m in members)),
                    ],
                    _DETAIL_WIDTHS,
                )
            )
            lines.append("")
    lines.append(f"Federal income tax withheld (Box 4)   {fmt(withheld)}")
    if state_withheld is not None:
        code, amount = state_withheld
        lines.append(f"State {code} State income tax withheld (Box 16)   {fmt(amount)}")
    return lines


def expected_capital_items(
    lots: Sequence[Lot],
    *,
    summary: bool,
    printed_net: dict[tuple[str, bool], int] | None = None,
) -> list[ParsedIncomeItem]:
    """Net gain/loss per term (summary-printed nets when a summary is printed, else the sum of
    the rows), short-term first."""
    printed_net = printed_net or {}
    items: list[ParsedIncomeItem] = []
    terms: list[tuple[str, IncomeCategory, str, str]] = [
        ("short", "short_term_capital_gain", "ST", "Net short-term gain or loss"),
        ("long", "long_term_capital_gain", "LT", "Net long-term gain or loss"),
    ]
    for term, category, box, label in terms:
        keys = [k for k in _groups(lots) if k[0] == term]
        if not keys:
            continue
        net = 0
        for key in keys:
            members = _groups(lots)[key]
            net += (
                printed_net.get(key, sum(m.gain for m in members))
                if summary
                else sum(m.gain for m in members)
            )
        items.append(ParsedIncomeItem(category, net, "1099-B", box, label))
    return items


def expected_withholding(
    withheld: int, state_withheld: tuple[str, int] | None
) -> list[ParsedTaxItem]:
    taxes: list[ParsedTaxItem] = []
    if withheld > 0:
        taxes.append(
            ParsedTaxItem(
                "federal_income_tax_withheld",
                withheld,
                "1099-B",
                "4",
                "Federal income tax withheld",
            )
        )
    if state_withheld is not None and state_withheld[1] > 0:
        taxes.append(
            ParsedTaxItem(
                "state_income_tax_withheld",
                state_withheld[1],
                "1099-B",
                "16",
                "State income tax withheld",
                jurisdiction=state_withheld[0],
            )
        )
    return taxes


def _lot(
    term: str,
    covered: bool,
    description: str,
    acquired: str,
    sold: str,
    proceeds: str,
    cost: str,
    wash: str = "0.00",
) -> Lot:
    return Lot(
        term, covered, description, acquired, sold, money(proceeds), money(cost), money(wash)
    )


_RECONCILE_WARNING = (
    "1099-B {term} figures do not reconcile: proceeds less cost basis plus wash sale "
    "adjustment differs from the printed gain or loss."
)


@dataclass(frozen=True, slots=True)
class _Spec:
    lots: tuple[Lot, ...]
    summary: bool
    detail: bool
    printed_net: dict[tuple[str, bool], int]
    withheld: int
    state_withheld: tuple[str, int] | None
    warnings: tuple[str, ...]


def _specs() -> dict[str, _Spec]:
    normal_lots = (
        _lot("short", True, "100 sh QRS CORP", "01/15/2024", "03/20/2024", "5,250.00", "4,800.00"),
        _lot("short", True, "40 sh TUV INC", "02/02/2024", "06/11/2024", "3,100.00", "2,950.00"),
        _lot("long", True, "250 sh WXY CO", "05/04/2022", "08/19/2024", "12,400.00", "8,100.00"),
        _lot("long", False, "75 sh LMN FUND", "11/30/2021", "09/09/2024", "2,000.00", "2,600.00"),
    )
    loss_lots = (
        _lot("short", True, "300 sh DEF CORP", "02/12/2024", "04/01/2024", "3,000.00", "5,500.00"),
        _lot("short", True, "50 sh GHI INC", "03/03/2024", "07/22/2024", "1,800.00", "2,500.50"),
        _lot("long", True, "10 sh JKL CO", "06/06/2021", "10/10/2024", "900.00", "650.00"),
    )
    wash_lots = (
        _lot(
            "short",
            True,
            "200 sh OPQ CORP",
            "04/10/2024",
            "05/15/2024",
            "2,000.00",
            "3,000.00",
            "400.00",
        ),
        _lot("short", True, "60 sh RST INC", "01/20/2024", "02/28/2024", "4,500.00", "4,100.00"),
        _lot("long", True, "90 sh UVW CO", "03/15/2020", "11/04/2024", "7,000.00", "5,250.00"),
    )
    mismatch_lots = (
        _lot("short", True, "100 sh QRS CORP", "01/15/2024", "03/20/2024", "5,250.00", "4,800.00"),
        _lot("long", True, "250 sh WXY CO", "05/04/2022", "08/19/2024", "12,400.00", "8,100.00"),
    )
    mismatch_net = {("short", True): money("550.00")}  # true gain is 450.00
    return {
        "normal_summary": _Spec(
            normal_lots, True, False, {}, money("150.00"), ("CA", money("45.00")), ()
        ),
        "transactions_only": _Spec(normal_lots, False, True, {}, 0, None, ()),
        "loss_year": _Spec(loss_lots, True, False, {}, 0, None, ()),
        "wash_sale": _Spec(wash_lots, True, True, {}, 0, None, ()),
        "mismatch": _Spec(
            mismatch_lots,
            True,
            False,
            mismatch_net,
            0,
            None,
            (_RECONCILE_WARNING.format(term="short-term"),),
        ),
    }


def identity_lines() -> list[str]:
    return [
        f"Form 1099-B  {B_TITLE}",
        f"Tax Year {TAX_YEAR}",
        f"Payer's name: {BROKER.name}",
        f"Payer's address: {', '.join(BROKER.address_lines)}",
        f"Payer's TIN: {BROKER.ein}",
        f"Recipient's name: {RECIPIENT_NAME}",
        f"Recipient's TIN: {RECIPIENT_SSN}",
        f"Account number: {ACCOUNT_NUMBER}",
        "CORRECTED (if checked)",
        "",
    ]


class Tax1099BBuilder:
    layout_id = "tax_1099_b"
    variants: tuple[str, ...] = (
        "normal_summary",
        "transactions_only",
        "loss_year",
        "wash_sale",
        "mismatch",
    )

    def build(self, out_dir: Path, *, variant: str, seed: int) -> dict[str, Any]:
        spec = _specs()[variant]
        body = section_lines(
            spec.lots,
            heading="Summary and detail of sales",
            summary=spec.summary,
            detail=spec.detail,
            printed_net=spec.printed_net,
            withheld=spec.withheld,
            state_withheld=spec.state_withheld,
        )
        pages = [[*identity_lines(), *body[2:]]]
        render_lines_pdf(out_dir / f"{self.layout_id}_{variant}.pdf", pages)
        form = ParsedTaxForm(
            form_type="1099-b",
            tax_year=TAX_YEAR,
            payer_name=BROKER.name,
            payer_tin_last4=last4(BROKER.ein),
            account_last4=last4(ACCOUNT_NUMBER),
            is_corrected=False,
            filing_status=None,
            agi_minor=None,
            taxable_income_minor=None,
            income=tuple(
                expected_capital_items(
                    spec.lots, summary=spec.summary, printed_net=spec.printed_net
                )
            ),
            taxes=tuple(expected_withholding(spec.withheld, spec.state_withheld)),
            warnings=spec.warnings,
        )
        doc = ParsedTaxDocument(tax_year=TAX_YEAR, forms=(form,))
        return build_golden(doc, expected_warnings=spec.warnings)


BUILDER = Tax1099BBuilder()
