"""Fixture builder for the `tax_1040` parser: a software-style Form 1040 (one visual row per line,
dot leaders, whole-dollar amounts printed with a trailing period) with the schedules that matter
attached (D, 1, 2, C).

Everything is invented (see `tax_common`). Amounts are internally consistent: line 16 is the
Qualified Dividends and Capital Gain Tax Worksheet result for the printed taxable income (from
`spend_analyzer.ingest.tax.estimate`, whose bracket data is hand-verified in its own tests), the
totals add up, and refund/owed equal payments minus total tax. Each variant's golden is built here
from the same figures, independently of the parser.

Variants: normal_wages_only, with_investment_income, high_income_niit, self_employed, refund,
owed, mfj, unsupported_year, no_schedule_d, missing_filing_status, refund_applied.

Wording follows the printed IRS form where it matters to the parser: line 16 carries the inline
"Check if any from Form(s): 1 [ ] 8814 2 [ ] 4972 3 [ ]" references and line 35a the "If Form 8888 is
attached, check here" text, so their form numbers sit between the label and the amount. In
`refund_applied` part of the overpayment is applied to next year's estimated tax (line 36), so the
refund received (line 35a) is less than the overpayment (line 34).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from spend_analyzer.core.tax_types import (
    IncomeCategory,
    ParsedIncomeItem,
    ParsedTaxDocument,
    ParsedTaxForm,
    ParsedTaxItem,
    TaxCategory,
)
from spend_analyzer.ingest.tax import estimate
from tests.fixtures.gen.base import render_lines_pdf
from tests.fixtures.gen.tax_common import (
    RECIPIENT_ADDRESS_LINES,
    RECIPIENT_SSN,
    SPOUSE_SSN,
    build_golden,
)

_FONT_SIZE = 7.5
_LINE_HEIGHT = 11.0
_LEFT_MARGIN = 36.0
_LABEL_WIDTH = 102  # characters up to the right-hand line reference
_COL_WIDTH = 50  # same, for two-column rows

STANDARD_DEDUCTION: dict[int, dict[str, int]] = {
    2022: {"single": 12_950, "mfj": 25_900, "mfs": 12_950, "hoh": 19_400, "qss": 25_900},
    2023: {"single": 13_850, "mfj": 27_700, "mfs": 13_850, "hoh": 20_800, "qss": 27_700},
    2024: {"single": 14_600, "mfj": 29_200, "mfs": 14_600, "hoh": 21_900, "qss": 29_200},
    2025: {"single": 15_750, "mfj": 31_500, "mfs": 15_750, "hoh": 23_625, "qss": 31_500},
}
SS_WAGE_BASE: dict[int, int] = {2022: 147_000, 2023: 160_200, 2024: 168_600, 2025: 176_100}
NIIT_THRESHOLD = {"single": 200_000, "mfj": 250_000, "mfs": 125_000, "hoh": 200_000, "qss": 250_000}

_STATUS_ROWS = (
    ("single", "Single"),
    ("mfj", "Married filing jointly (even if only one had income)"),
    ("mfs", "Married filing separately (MFS). Enter spouse's name here"),
    ("hoh", "Head of household (HOH). Enter the child's name if the qualifying person is a child"),
    ("qss", "Qualifying surviving spouse (QSS)"),
)

_VARIANTS = (
    "normal_wages_only",
    "with_investment_income",
    "high_income_niit",
    "self_employed",
    "refund",
    "owed",
    "mfj",
    "unsupported_year",
    "no_schedule_d",
    "missing_filing_status",
    "refund_applied",
)


@dataclass(frozen=True)
class _Spec:
    """Inputs of one fixture return, whole dollars."""

    year: int
    status: str | None  # None => no box checked
    wages: int = 0
    interest: int = 0
    ordinary_dividends: int = 0
    qualified_dividends: int = 0
    short_term: int = 0  # Schedule D line 7
    long_term: int = 0  # Schedule D line 15 (includes capital gain distributions)
    schedule_d: bool = False
    line7_only: int = 0  # 1040 line 7 when no Schedule D is attached
    business: int = 0  # Schedule C net profit
    business_expenses: int = 13_000
    child_credit: int = 0
    estimated_payments: int = 0
    withheld_1099: int = 0
    delta: int = 0  # payments - total tax (positive: refund, negative: owed)
    applied: int = 0  # part of the overpayment applied to next year's estimated tax (line 36)
    niit: bool = False
    additional_medicare: bool = False
    force_schedule_2: bool = False
    show_zero_lines: frozenset[str] = frozenset()
    calc_year: int | None = None  # tables used for the arithmetic when `year` has none
    spouse: bool = False
    dependents: tuple[str, ...] = ()


_SPECS: dict[str, _Spec] = {
    "normal_wages_only": _Spec(2024, "single", wages=85_000, delta=812),
    "with_investment_income": _Spec(
        2024,
        "single",
        wages=90_000,
        interest=1_850,
        ordinary_dividends=4_200,
        qualified_dividends=3_100,
        short_term=1_500,
        long_term=12_000,
        schedule_d=True,
        force_schedule_2=True,
        show_zero_lines=frozenset({"niit"}),
        withheld_1099=150,
        delta=300,
    ),
    "high_income_niit": _Spec(
        2024,
        "single",
        wages=310_000,
        interest=8_000,
        ordinary_dividends=20_000,
        qualified_dividends=15_000,
        short_term=5_000,
        long_term=60_000,
        schedule_d=True,
        niit=True,
        additional_medicare=True,
        delta=-1_800,
    ),
    "self_employed": _Spec(
        2024,
        "single",
        wages=20_000,
        interest=100,
        business=62_000,
        estimated_payments=6_000,
        delta=-2_150,
    ),
    "refund": _Spec(2023, "mfs", wages=62_500, interest=120, delta=3_150),
    "owed": _Spec(2022, "hoh", wages=58_000, interest=300, delta=-2_415),
    "mfj": _Spec(
        2025,
        "mfj",
        wages=148_000,
        interest=900,
        ordinary_dividends=2_300,
        qualified_dividends=2_000,
        long_term=6_000,
        schedule_d=True,
        child_credit=4_400,
        spouse=True,
        dependents=("Casey Testperson", "Morgan Testperson"),
        delta=500,
    ),
    "unsupported_year": _Spec(2027, "single", wages=72_000, calc_year=2025, delta=100),
    "no_schedule_d": _Spec(
        2024,
        "qss",
        wages=52_000,
        interest=400,
        ordinary_dividends=600,
        qualified_dividends=600,
        line7_only=1_200,
        delta=640,
    ),
    "missing_filing_status": _Spec(2024, None, wages=68_000, delta=-420),
    "refund_applied": _Spec(2024, "single", wages=74_000, interest=250, delta=2_000, applied=500),
}


# --------------------------------------------------------------------------------------------
# Arithmetic
# --------------------------------------------------------------------------------------------


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class _Calc:
    wages: int
    interest: int
    ordinary_dividends: int
    qualified_dividends: int
    short_term: int
    long_term: int
    line16_gain: int  # Schedule D line 16
    line7: int  # 1040 line 7
    business: int
    line8: int
    total_income: int
    adjustments: int
    agi: int
    deduction: int
    taxable: int
    net_ltcg: int  # preferential gain fed to the worksheet
    tax: int  # line 16
    credits: int
    se_tax: int
    additional_medicare: int
    niit: int
    other_taxes: int
    total_tax: int
    withheld_w2: int
    withheld_1099: int
    withheld_total: int
    estimated: int
    total_payments: int
    refund: int  # line 34, the overpayment
    applied: int  # line 36
    owed: int


def _calc(spec: _Spec) -> _Calc:
    table_year = spec.calc_year or spec.year
    status = spec.status or "single"
    if spec.schedule_d:
        gain16 = spec.short_term + spec.long_term
        line7 = gain16 if gain16 >= 0 else max(gain16, -3_000)
        net_ltcg = max(0, min(spec.long_term, gain16))
    else:
        gain16 = 0
        line7 = spec.line7_only
        net_ltcg = max(0, spec.line7_only)
    se_tax = 0
    adjustments = 0
    if spec.business:
        earnings = Decimal(spec.business) * Decimal("0.9235")
        ss_room = max(SS_WAGE_BASE[table_year] - spec.wages, 0)
        se_tax = _round(
            min(earnings, Decimal(ss_room)) * Decimal("0.124") + earnings * Decimal("0.029")
        )
        adjustments = se_tax // 2
    line8 = spec.business
    total_income = spec.wages + spec.interest + spec.ordinary_dividends + line7 + line8
    agi = total_income - adjustments
    deduction = STANDARD_DEDUCTION[table_year][status]
    taxable = max(agi - deduction, 0)
    tax_minor = estimate.worksheet_tax(
        tax_year=table_year,
        filing_status=status,
        taxable_income_minor=taxable * 100,
        qualified_dividends_minor=spec.qualified_dividends * 100,
        net_ltcg_minor=net_ltcg * 100,
    )
    assert tax_minor is not None
    tax = tax_minor // 100
    add_medicare = 0
    if spec.additional_medicare:
        add_medicare = _round(
            Decimal(max(spec.wages - NIIT_THRESHOLD[status], 0)) * Decimal("0.009")
        )
    niit = 0
    if spec.niit:
        nii = spec.interest + spec.ordinary_dividends + max(line7, 0)
        niit = _round(Decimal(min(nii, max(agi - NIIT_THRESHOLD[status], 0))) * Decimal("0.038"))
    other_taxes = se_tax + add_medicare + niit
    credits = min(spec.child_credit, tax)
    total_tax = max(tax - credits, 0) + other_taxes
    payments = total_tax + spec.delta
    withheld_total = payments - spec.estimated_payments
    withheld_w2 = withheld_total - spec.withheld_1099
    return _Calc(
        wages=spec.wages,
        interest=spec.interest,
        ordinary_dividends=spec.ordinary_dividends,
        qualified_dividends=spec.qualified_dividends,
        short_term=spec.short_term,
        long_term=spec.long_term,
        line16_gain=gain16,
        line7=line7,
        business=spec.business,
        line8=line8,
        total_income=total_income,
        adjustments=adjustments,
        agi=agi,
        deduction=deduction,
        taxable=taxable,
        net_ltcg=net_ltcg,
        tax=tax,
        credits=credits,
        se_tax=se_tax,
        additional_medicare=add_medicare,
        niit=niit,
        other_taxes=other_taxes,
        total_tax=total_tax,
        withheld_w2=withheld_w2,
        withheld_1099=spec.withheld_1099,
        withheld_total=withheld_total,
        estimated=spec.estimated_payments,
        total_payments=payments,
        refund=max(spec.delta, 0),
        applied=spec.applied,
        owed=max(-spec.delta, 0),
    )


# --------------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------------


def _amount(value: int | None, *, show_zero: bool = True) -> str:
    """Whole-dollar amount as tax software prints it: ``85,000.``, negatives in parentheses,
    blank for an empty line."""
    if value is None or (value == 0 and not show_zero):
        return ""
    return f"({abs(value):,}.)" if value < 0 else f"{value:,}."


def _dots(text: str, width: int) -> str:
    padded = text + " "
    fill = max(width - len(padded), 0)
    return (padded + ". " * (fill // 2)).ljust(width)


def _line(num: str, text: str, ref: str | None, value: str) -> str:
    """One full-width line: ``<num> <label> . . . . <ref> <value>``."""
    left = _dots(f"{num:<4}{text}", _LABEL_WIDTH)
    return f"{left}{(ref or num):<5}{value:>10}".rstrip()


def _line2(
    num_a: str, text_a: str, value_a: str, marker: str, text_b: str, ref_b: str, value_b: str
) -> str:
    """Two-column row (``2a ... b ...``) as on the printed form."""
    left = _dots(f"{num_a:<4}{text_a}", _COL_WIDTH - 15) + f"{num_a:<5}{value_a:>9}"
    right = _dots(f"{marker} {text_b}", _COL_WIDTH - 5) + f"{ref_b:<5}{value_b:>9}"
    return f"{left}  {right}".rstrip()


def _heading(year: int, page: int) -> list[str]:
    if page == 1:
        return [
            f"Form 1040  U.S. Individual Income Tax Return  {year}",
            "Department of the Treasury - Internal Revenue Service   OMB No. 1545-0074   IRS Use Only",
            f"For the year Jan. 1 - Dec. 31, {year}, or other tax year beginning ________, {year},"
            f" ending ________, 20__",
        ]
    return [f"Form 1040 ({year})  Page 2"]


def _status_rows(status: str | None) -> list[str]:
    rows = []
    for i, (key, label) in enumerate(_STATUS_ROWS):
        mark = "[X]" if key == status else "[ ]"
        prefix = "Filing Status  " if i == 0 else ""
        rows.append(f"{prefix}{mark} {label}")
    return rows


def _page_one(spec: _Spec, c: _Calc) -> list[str]:
    y = spec.year
    z = {"show_zero": False}
    lines = _heading(y, 1)
    lines += [
        "Your first name and middle initial  Jordan Q.    Last name  Testperson    "
        f"Your social security number  {RECIPIENT_SSN}",
    ]
    if spec.spouse:
        lines.append(
            "If joint return, spouse's first name  Riley M.    Last name  Testperson    "
            f"Spouse's social security number  {SPOUSE_SSN}"
        )
    lines += [
        f"Home address (number and street)  {RECIPIENT_ADDRESS_LINES[0]}",
        "City, town, or post office  Testville    State  ZZ    ZIP code  00000",
        f"Digital Assets  At any time during {y}, did you receive or dispose of a digital asset?"
        "  [ ] Yes  [X] No",
        "",
        *_status_rows(spec.status),
        "",
    ]
    if spec.dependents:
        lines.append(
            "Dependents (see instructions)  First name  Last name  Relationship  Child tax credit"
        )
        lines += [f"  {name}  Child  [X]" for name in spec.dependents]
        lines.append("")
    lines.append("Income")
    if y == 2022:
        lines.append(
            _line("1", "Wages, salaries, tips, etc. Attach Form(s) W-2", None, _amount(c.wages))
        )
    else:
        lines += [
            _line(
                "1a",
                "Total amount from Form(s) W-2, box 1 (see instructions)",
                None,
                _amount(c.wages),
            ),
            _line("1b", "Household employee wages not reported on Form(s) W-2", None, ""),
            _line("1c", "Tip income not reported on line 1a (see instructions)", None, ""),
            _line("1d", "Medicaid waiver payments not reported on Form(s) W-2", None, ""),
            _line("1e", "Taxable dependent care benefits from Form 2441, line 26", None, ""),
            _line("1f", "Employer-provided adoption benefits from Form 8839, line 29", None, ""),
            _line("1g", "Wages from Form 8919, line 6", None, ""),
            _line("1h", "Other earned income (see instructions)", None, ""),
            _line("1i", "Nontaxable combat pay election (see instructions)", None, ""),
            _line("1z", "Add lines 1a through 1h", None, _amount(c.wages)),
        ]
    lines += [
        _line2(
            "2a", "Tax-exempt interest", "", "b", "Taxable interest", "2b", _amount(c.interest, **z)
        ),
        _line2(
            "3a",
            "Qualified dividends",
            _amount(c.qualified_dividends, **z),
            "b",
            "Ordinary dividends",
            "3b",
            _amount(c.ordinary_dividends, **z),
        ),
        _line2("4a", "IRA distributions", "", "b", "Taxable amount", "4b", ""),
        _line2("5a", "Pensions and annuities", "", "b", "Taxable amount", "5b", ""),
        _line2("6a", "Social security benefits", "", "b", "Taxable amount", "6b", ""),
        _line(
            "7",
            "Capital gain or (loss). Attach Schedule D if required",
            None,
            _amount(c.line7, **z),
        ),
        _line("8", "Additional income from Schedule 1, line 10", None, _amount(c.line8, **z)),
        _line(
            "9",
            "Add lines 1z, 2b, 3b, 4b, 5b, 6b, 7, and 8. This is your total income"
            if y != 2022
            else "Add lines 1, 2b, 3b, 4b, 5b, 6b, 7, and 8. This is your total income",
            None,
            _amount(c.total_income),
        ),
        _line(
            "10",
            "Adjustments to income from Schedule 1, line 26",
            None,
            _amount(c.adjustments, **z),
        ),
        _line(
            "11a" if y >= 2025 else "11",
            "Subtract line 10 from line 9. This is your adjusted gross income",
            None,
            _amount(c.agi),
        ),
    ]
    return lines


def _page_two(spec: _Spec, c: _Calc) -> list[str]:
    y = spec.year
    z = {"show_zero": False}
    lines = _heading(y, 2)
    lines.append(f"Your name  Jordan Q. Testperson    Your social security number  {RECIPIENT_SSN}")
    if y >= 2025:
        lines.append(
            _line("11b", "Amount from line 11a (adjusted gross income)", None, _amount(c.agi))
        )
        lines += [
            _line(
                "12e",
                "Standard deduction or itemized deductions (from Schedule A)",
                None,
                _amount(c.deduction),
            ),
            _line(
                "13a",
                "Qualified business income deduction from Form 8995 or Form 8995-A",
                None,
                "",
            ),
            _line("13b", "Additional deductions from Schedule 1-A, line 38", None, ""),
            _line("14", "Add lines 12e, 13a, and 13b", None, _amount(c.deduction)),
        ]
    else:
        lines += [
            _line(
                "12",
                "Standard deduction or itemized deductions (from Schedule A)",
                None,
                _amount(c.deduction),
            ),
            _line(
                "13",
                "Qualified business income deduction from Form 8995 or Form 8995-A",
                None,
                "",
            ),
            _line("14", "Add lines 12 and 13", None, _amount(c.deduction)),
        ]
    lines += [
        _line(
            "15",
            "Subtract line 14 from line 11. If zero or less, enter -0-",
            None,
            _amount(c.taxable),
        ),
        "Tax and Credits",
        _line(
            "16",
            "Tax (see instructions). Check if any from Form(s): 1 [ ] 8814 2 [ ] 4972 3 [ ]",
            None,
            _amount(c.tax),
        ),
        _line("17", "Amount from Schedule 2, line 3", None, ""),
        _line("18", "Add lines 16 and 17", None, _amount(c.tax)),
        _line(
            "19",
            "Nonrefundable child tax credit or credit for other dependents from Schedule 8812",
            None,
            _amount(c.credits, **z),
        ),
        _line("20", "Amount from Schedule 3, line 8", None, ""),
        _line("21", "Add lines 19 and 20", None, _amount(c.credits, **z)),
        _line(
            "22",
            "Subtract line 21 from line 18. If zero or less, enter -0-",
            None,
            _amount(max(c.tax - c.credits, 0)),
        ),
        _line(
            "23",
            "Other taxes, including self-employment tax, from Schedule 2, line 21",
            None,
            _amount(c.other_taxes, **z),
        ),
        _line("24", "Add lines 22 and 23. This is your total tax", None, _amount(c.total_tax)),
        "Payments",
        _line("25a", "Form(s) W-2", None, _amount(c.withheld_w2, **z)),
        _line("25b", "Form(s) 1099", None, _amount(c.withheld_1099, **z)),
        _line("25c", "Other forms (see instructions)", None, ""),
        _line("25d", "Add lines 25a through 25c", None, _amount(c.withheld_total, **z)),
        _line(
            "26",
            f"{y} estimated tax payments and amount applied from {y - 1} return",
            None,
            _amount(c.estimated, **z),
        ),
        _line("27", "Earned income credit (EIC)", None, ""),
        _line(
            "32",
            "Add lines 27, 28, 29, and 31. These are your total other payments and refundable credits",
            None,
            "",
        ),
        _line(
            "33",
            "Add lines 25d, 26, and 32. These are your total payments",
            None,
            _amount(c.total_payments),
        ),
        "Refund",
        _line(
            "34",
            "If line 33 is more than line 24, subtract line 24 from line 33. This is the amount you overpaid",
            None,
            _amount(c.refund, **z),
        ),
        _line(
            "35a",
            "Amount of line 34 you want refunded to you. If Form 8888 is attached, check here [ ]",
            None,
            _amount(c.refund - c.applied, **z),
        ),
        _line(
            "36",
            f"Amount of line 34 you want applied to your {y + 1} estimated tax",
            None,
            _amount(c.applied, **z),
        ),
        "Amount You Owe",
        _line(
            "37",
            "Subtract line 33 from line 24. This is the amount you owe",
            None,
            _amount(c.owed, **z),
        ),
        _line("38", "Estimated tax penalty (see instructions)", None, ""),
        "Sign Here  Under penalties of perjury, I declare that I have examined this return.",
    ]
    return lines


def _schedule_d(spec: _Spec, c: _Calc) -> list[str]:
    y = spec.year
    return [
        "SCHEDULE D (Form 1040)  Capital Gains and Losses",
        f"Attach to Form 1040 or 1040-SR.  Attachment Sequence No. 12A  {y}",
        f"Name(s) shown on return  Jordan Q. Testperson    Your social security number  {RECIPIENT_SSN}",
        "Part I  Short-Term Capital Gains and Losses - Assets or Investments Held 1 Year or Less",
        _line(
            "7",
            "Net short-term capital gain or (loss). Combine lines 1a through 6 in column (h)",
            None,
            _amount(c.short_term, show_zero=True),
        ),
        "Part II  Long-Term Capital Gains and Losses - Assets or Investments Held More Than 1 Year",
        _line(
            "15",
            "Net long-term capital gain or (loss). Combine lines 8a through 14 in column (h)",
            None,
            _amount(c.long_term, show_zero=True),
        ),
        "Part III  Summary",
        _line(
            "16",
            "Combine lines 7 and 15 and enter the result",
            None,
            _amount(c.line16_gain, show_zero=True),
        ),
        _line("18", "Amount from the 28% Rate Gain Worksheet (see instructions)", None, "0."),
        _line(
            "19",
            "Amount from the Unrecaptured Section 1250 Gain Worksheet (see instructions)",
            None,
            "0.",
        ),
    ]


def _schedule_1(spec: _Spec, c: _Calc) -> list[str]:
    return [
        "SCHEDULE 1 (Form 1040)  Additional Income and Adjustments to Income",
        f"Attach to Form 1040, 1040-SR, or 1040-NR.  {spec.year}",
        _line("3", "Business income or (loss). Attach Schedule C", None, _amount(c.business)),
        _line(
            "10",
            "Combine lines 1 through 7 and 9. This is your additional income",
            None,
            _amount(c.line8),
        ),
        _line(
            "15",
            "Deductible part of self-employment tax. Attach Schedule SE",
            None,
            _amount(c.adjustments),
        ),
        _line(
            "26",
            "Add lines 11 through 23 and 25. These are your total adjustments to income",
            None,
            _amount(c.adjustments),
        ),
    ]


def _schedule_c(spec: _Spec, c: _Calc) -> list[str]:
    gross = c.business + spec.business_expenses
    return [
        "SCHEDULE C (Form 1040)  Profit or Loss From Business (Sole Proprietorship)",
        f"Attach to Form 1040, 1040-SR, 1040-NR, or 1041.  {spec.year}",
        "A  Principal business or profession, including product or service  Freelance design",
        _line("1", "Gross receipts or sales", None, _amount(gross)),
        _line("7", "Gross income. Add lines 5 and 6", None, _amount(gross)),
        _line(
            "28",
            "Total expenses before expenses for business use of home. Add lines 8 through 27a",
            None,
            _amount(spec.business_expenses),
        ),
        _line(
            "29",
            "Tentative profit or (loss). Subtract line 28 from line 7",
            None,
            _amount(c.business),
        ),
        _line(
            "31",
            "Net profit or (loss). Subtract line 30 from line 29.",
            None,
            _amount(c.business),
        ),
    ]


def _schedule_2(spec: _Spec, c: _Calc) -> list[str]:
    show = spec.show_zero_lines

    def val(amount: int, key: str) -> str:
        return _amount(amount, show_zero=key in show)

    return [
        "SCHEDULE 2 (Form 1040)  Additional Taxes",
        f"Attach to Form 1040, 1040-SR, or 1040-NR.  {spec.year}",
        "Part I  Tax",
        _line("3", "Add lines 1 and 2. Enter here and include on Form 1040, line 17", None, ""),
        "Part II  Other Taxes",
        _line("4", "Self-employment tax. Attach Schedule SE", None, val(c.se_tax, "se")),
        _line(
            "11",
            "Additional Medicare Tax. Attach Form 8959",
            None,
            val(c.additional_medicare, "medicare"),
        ),
        _line("12", "Net investment income tax. Attach Form 8960", None, val(c.niit, "niit")),
        _line(
            "21",
            "Add lines 4, 7 through 16, and 18. These are your total other taxes",
            None,
            _amount(c.other_taxes, show_zero=False),
        ),
    ]


def _pages(spec: _Spec, c: _Calc) -> list[list[str]]:
    pages = [_page_one(spec, c), _page_two(spec, c)]
    if spec.business:
        pages.append(_schedule_1(spec, c))
        pages.append(_schedule_c(spec, c))
    if spec.schedule_d:
        pages.append(_schedule_d(spec, c))
    if spec.business or spec.niit or spec.additional_medicare or spec.force_schedule_2:
        pages.append(_schedule_2(spec, c))
    return pages


# --------------------------------------------------------------------------------------------
# Golden
# --------------------------------------------------------------------------------------------

_Cents = 100


def _inc(
    category: IncomeCategory, dollars: int, box: str, label: str, *, memo: bool = False
) -> ParsedIncomeItem | None:
    if dollars == 0:
        return None
    return ParsedIncomeItem(category, dollars * _Cents, "1040", box, label, memo)


def _tax(
    category: TaxCategory,
    dollars: int,
    box: str,
    label: str,
    *,
    keep_zero: bool = False,
    estimate_: bool = False,
) -> ParsedTaxItem | None:
    if dollars == 0 and not keep_zero:
        return None
    return ParsedTaxItem(category, dollars * _Cents, "1040", box, label, None, estimate_)


def _golden_doc(spec: _Spec, c: _Calc) -> tuple[ParsedTaxDocument, list[str]]:
    warnings: list[str] = []
    if spec.year not in estimate.supported_years():
        warnings.append(f"form layout for tax year {spec.year} not verified")

    wages_box = "line 1" if spec.year == 2022 else "line 1z"
    if spec.schedule_d:
        gains = [
            _inc(
                "short_term_capital_gain",
                c.short_term,
                "Schedule D line 7",
                "Net short-term capital gain or loss",
            ),
            _inc(
                "long_term_capital_gain",
                c.long_term,
                "Schedule D line 15",
                "Net long-term capital gain or loss",
            ),
        ]
    else:
        gains = [_inc("long_term_capital_gain", c.line7, "line 7", "Capital gain or loss")]
    income = [
        _inc("salary_wages", c.wages, wages_box, "Wages, salaries, tips"),
        _inc("interest", c.interest, "line 2b", "Taxable interest"),
        _inc("ordinary_dividends", c.ordinary_dividends, "line 3b", "Ordinary dividends"),
        _inc(
            "qualified_dividends",
            c.qualified_dividends,
            "line 3a",
            "Qualified dividends",
            memo=True,
        ),
        *gains,
        _inc("self_employment", c.business, "Schedule 1 line 3", "Business income"),
    ]

    def sch2(key: str, amount: int) -> bool:
        return amount != 0 or key in spec.show_zero_lines

    has_sch2 = bool(spec.business or spec.niit or spec.additional_medicare or spec.force_schedule_2)
    taxes = [
        _tax("total_federal_tax", c.total_tax, "line 24", "Total tax", keep_zero=True),
        _tax(
            "federal_income_tax_withheld",
            c.withheld_total,
            "line 25d",
            "Federal income tax withheld",
        ),
        _tax("estimated_tax_payments", c.estimated, "line 26", "Estimated tax payments"),
        _tax("total_payments", c.total_payments, "line 33", "Total payments", keep_zero=True),
        _tax("refund", c.refund - c.applied, "line 35a", "Refund received"),
        _tax("amount_owed", c.owed, "line 37", "Amount you owe"),
    ]
    if has_sch2:
        taxes += [
            _tax(
                "net_investment_income_tax",
                c.niit,
                "Schedule 2 line 12",
                "Net investment income tax",
                keep_zero=True,
            )
            if sch2("niit", c.niit)
            else None,
            _tax(
                "additional_medicare_tax",
                c.additional_medicare,
                "Schedule 2 line 11",
                "Additional Medicare Tax",
                keep_zero=True,
            )
            if sch2("medicare", c.additional_medicare)
            else None,
            _tax(
                "self_employment_tax",
                c.se_tax,
                "Schedule 2 line 4",
                "Self-employment tax",
                keep_zero=True,
            )
            if sch2("se", c.se_tax)
            else None,
        ]

    if spec.status is None:
        warnings.append("filing status not found; federal tax split not estimated")
    elif spec.year not in estimate.supported_years():
        warnings.append(
            f"no federal bracket table for tax year {spec.year}; tax split not estimated"
        )
    else:
        split = estimate.estimate_tax_split(
            tax_year=spec.year,
            filing_status=spec.status,
            taxable_income_minor=c.taxable * _Cents,
            qualified_dividends_minor=c.qualified_dividends * _Cents,
            net_ltcg_minor=c.net_ltcg * _Cents,
            income_tax_minor=c.tax * _Cents,
        )
        assert split is not None and not split.approximate
        taxes += [
            ParsedTaxItem(
                "federal_tax_ordinary_est",
                split.ordinary_minor,
                "1040",
                "line 16",
                "Tax on ordinary income (estimate)",
                None,
                True,
            ),
            ParsedTaxItem(
                "federal_tax_preferential_est",
                split.preferential_minor,
                "1040",
                "line 16",
                "Tax on qualified dividends and gains (estimate)",
                None,
                True,
            ),
        ]

    form = ParsedTaxForm(
        form_type="1040",
        tax_year=spec.year,
        payer_name=None,
        payer_tin_last4=None,
        account_last4=None,
        is_corrected=False,
        filing_status=spec.status,
        agi_minor=c.agi * _Cents,
        taxable_income_minor=c.taxable * _Cents,
        income=tuple(i for i in income if i is not None),
        taxes=tuple(t for t in taxes if t is not None),
        warnings=tuple(warnings),
    )
    return ParsedTaxDocument(tax_year=spec.year, forms=(form,), warnings=()), warnings


# --------------------------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------------------------


class Tax1040Builder:
    """`LayoutBuilder` for the 1040 parser (`tax_1040`)."""

    layout_id = "tax_1040"
    variants = _VARIANTS

    def build(self, out_dir: Path, *, variant: str, seed: int) -> dict[str, Any]:
        """Render ``out_dir/tax_1040_<variant>.pdf`` and return its golden (`build_golden`)."""
        if variant not in _SPECS:
            raise ValueError(f"unknown tax_1040 variant: {variant}")
        spec = _SPECS[variant]
        calc = _calc(spec)
        render_lines_pdf(
            out_dir / f"{self.layout_id}_{variant}.pdf",
            _pages(spec, calc),
            font_size=_FONT_SIZE,
            line_height=_LINE_HEIGHT,
            left_margin=_LEFT_MARGIN,
            top_margin=44.0,
        )
        doc, warnings = _golden_doc(spec, calc)
        return build_golden(doc, expected_warnings=warnings)


BUILDER = Tax1040Builder()


def spec_for(variant: str) -> tuple[_Spec, _Calc]:
    """The figures behind a variant (for tests that assert on arithmetic)."""
    spec = _SPECS[variant]
    return spec, _calc(spec)
