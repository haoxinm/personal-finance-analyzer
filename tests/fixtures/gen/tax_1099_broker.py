"""Synthetic fixture generator for a landscape broker tax package
(layout id ``tax_1099_broker``).

Unlike ``tax_1099_consolidated`` (one tidy column of ``label  amount`` rows) this layout mimics the
hard parts of real brokerage statements, all invented here:

* landscape pages with TWO forms side by side (``Form 1099-DIV`` left, ``Form 1099-INT`` right)
  and two summary blocks side by side beneath them, so plain text lines interleave both;
* letter-spaced headings (``F o r m  1 0 9 9 - D I V``), ``(cid:N)`` glyph junk, dot leaders
  between a label and its amount, ``1a Total ...`` box prefixes, a state code on its own row;
* a repeated page header (``Account No.`` / ``Page n of m``) on every page and a payer block
  printed beside the recipient block;
* a ``Form 1099-MISC`` block with an un-headed table beside it, mostly zero values;
* a page of instructions to the recipient that names every form type but carries no amount;
* a 1099-B transaction table over three pages with per-security subtotals, covered / noncovered
  category subtotals, term net lines, a statement total and a recap, plus a transaction whose
  gain is ``N/C``.

Variants: ``two_up`` (everything above), ``b_category_subtotals`` (1099-B without term
net lines or recap: the totals must be derived from the category subtotals), ``b_detail_only``
(only trades and security subtotals: derived from the trades, never double counting the
subtotals), ``zero_sections`` (every box zero: recognised, not "unrecognised") and
``section_continued`` (the DIV section continues on page 2 under ``Form 1099-DIV (continued)``).
Each yields ONE ``1099-consolidated`` form.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import landscape, letter

from personal_finance_analyzer.core.tax_types import (
    ParsedIncomeItem,
    ParsedTaxDocument,
    ParsedTaxForm,
    ParsedTaxItem,
)
from tests.fixtures.gen.tax_common import (
    BROKER,
    RECIPIENT_ADDRESS_LINES,
    RECIPIENT_NAME,
    BoxItem,
    build_golden,
    last4,
    money,
    render_boxes_pdf,
)

YEAR = 2024
ACCOUNT = "SYN-4455-7731"
_LEFT, _RIGHT = 26.0, 426.0
_COL_CHARS = 60
_PITCH = 9.0
_NC_WARNING = "Some 1099-B transactions have no computed gain or loss and are not included."


def fmt(cents: int) -> str:
    """``-1500`` -> ``(15.00)``; ``123456`` -> ``1,234.56``."""
    text = f"{abs(cents) // 100:,}.{abs(cents) % 100:02d}"
    return f"({text})" if cents < 0 else text


def spaced(text: str) -> str:
    """Letter-space ``text``: single spaces between letters, three between words."""
    return "   ".join(" ".join(word) for word in text.split(" "))


def leader(label: str, amount: str, chars: int = _COL_CHARS) -> str:
    """``label ........ amount`` padded with dot leaders to ``chars`` characters."""
    dots = max(3, chars - len(label) - len(amount) - 2)
    return f"{label} {'.' * dots} {amount}"


@dataclass(frozen=True, slots=True)
class Lot:
    desc: str
    qty: str
    acquired: str
    sold: str
    proceeds: int
    cost: int
    wash: int = 0
    footnote: str = ""
    not_computed: bool = False

    @property
    def gain(self) -> int:
        return self.proceeds - self.cost + self.wash

    def row(self) -> str:
        gain = "N/C" if self.not_computed else fmt(self.gain)
        mid = f"{fmt(self.proceeds)} {fmt(self.cost)}"
        note = f" {self.footnote}" if self.footnote else ""
        wash = "0.00" if not self.wash else fmt(self.wash)
        return f"{self.qty} Sale {self.acquired} {self.sold} {mid}{note} 0.00 {wash} {gain}"


@dataclass(frozen=True, slots=True)
class Spec:
    div: tuple[str, str, str, str, str, str] = (
        "2,450.75",
        "1,900.25",
        "310.40",
        "45.00",
        "25.00",
        "3.25",
    )  # 1a, 1b, 2a, 12, federal withheld, OH withheld
    interest: tuple[str, str, str, str, str] = ("1,234.56", "210.00", "80.00", "12.00", "7.50")
    # 1, 3, 8, federal withheld, CA withheld
    misc: tuple[str, str] = ("0.00", "0.00")  # royalties, other income
    oid: tuple[str, str] = ("0.00", "15.00")  # OID, OID on Treasury obligations
    st_covered: tuple[Lot, ...] = (
        Lot("ALPHA WIDGET CORP", "100.0000", "01/15/24", "03/20/24", 525_000, 480_000),
        Lot("ALPHA WIDGET CORP", "300.0000", "02/12/24", "04/01/24", 300_000, 550_000),
        Lot(
            "BETA UTILITIES INC", "50.0000", "02/01/24", "05/06/24", 200_000, 260_000, 15_000, "(W)"
        ),
    )
    st_noncovered: tuple[Lot, ...] = (
        Lot("GAMMA BOND FUND", "75.0000", "03/03/23", "06/10/24", 1_000_000, 900_000),
    )
    lt_covered: tuple[Lot, ...] = (
        Lot("DELTA HOLDINGS CO", "250.0000", "05/04/22", "08/19/24", 1_240_000, 810_000),
        Lot("EPSILON TRUST UNITS", "20.0000", "11/30/21", "09/09/24", 100_000, 150_000),
    )
    other: tuple[Lot, ...] = (
        Lot("ZETA PARTNERS LP", "40.0000", "06/01/20", "10/10/24", 800_000, 0, not_computed=True),
    )
    b_mode: str = "full"  # full | category_subtotals | detail_only
    show_b: bool = True
    continued_div: bool = False


def _net(lots: tuple[Lot, ...]) -> int:
    return sum(lot.gain for lot in lots)


# --------------------------------------------------------------------------------------------
# Page building
# --------------------------------------------------------------------------------------------


class _Page:
    def __init__(self, number: int, total: int) -> None:
        self.items: list[BoxItem] = []
        self.number = number
        self.total = total

    def put(self, x: float, y: float, text: str) -> None:
        self.items.append((x, y, text))

    def header(self) -> None:
        self.put(250.0, 30.0, "Account No.            Taxpayer No.           Page")
        self.put(
            250.0,
            40.0,
            f"{ACCOUNT}        XXX-XX-9999        (cid:5)(cid:5)(cid:5){self.number} of {self.total}",
        )
        self.put(
            24.0, 52.0, "(cid:9)(cid:9)(cid:9)(cid:9)(cid:9)(cid:9)(cid:9)(cid:9)(cid:9)(cid:9)"
        )

    def title(self, y: float) -> None:
        self.put(24.0, y, RECIPIENT_NAME)
        self.put(300.0, y + 6.0, f"{YEAR} TAX REPORTING STATEMENT")


def _page1(spec: Spec, total: int) -> tuple[_Page, list[ParsedIncomeItem], list[ParsedTaxItem]]:
    page = _Page(1, total)
    page.header()
    # recipient block (left) and payer block (right) share rows
    page.put(24.0, 76.0, RECIPIENT_NAME)
    page.put(150.0, 76.0, "Your Financial Contact")
    page.put(_RIGHT, 76.0, "Payer:")
    page.put(24.0, 85.0, RECIPIENT_ADDRESS_LINES[0])
    page.put(_RIGHT, 85.0, BROKER.name)
    page.put(24.0, 94.0, RECIPIENT_ADDRESS_LINES[1])
    page.put(_RIGHT, 94.0, BROKER.address_lines[0])
    page.put(_RIGHT, 103.0, BROKER.address_lines[1])
    page.put(_RIGHT, 112.0, f"Federal Identification Number {BROKER.ein}")
    page.put(300.0, 130.0, f"{YEAR} TAX REPORTING STATEMENT")

    income: list[ParsedIncomeItem] = []
    taxes: list[ParsedTaxItem] = []
    a1, b1, a2, b12, dfed, dstate = spec.div
    page.put(
        24.0,
        150.0,
        "Form " + spaced("1099-DIV") + f"  {YEAR}  " + spaced("Dividends and Distributions"),
    )
    left_rows = [
        ("1a Total Ordinary Dividends", a1),
        ("1b Qualified Dividends", b1),
        ("2a Total Capital Gain Distributions", a2),
        ("2b Unrecap. Sec. 1250 Gain", "0.00"),
        ("2c Section 1202 Gain", "0.00"),
        ("3 Nondividend Distributions", "0.00"),
        ("4 Federal Income Tax Withheld", dfed),
        ("5 Section 199A Dividends", "0.00"),
        ("7 Foreign Tax Paid", "0.00"),
        ("12 Exempt-Interest Dividends", b12),
        ("14 State", "OH"),
        ("16 State Tax Withheld", dstate),
    ]
    if spec.continued_div:  # box 12 is printed on page 2 under the "(continued)" heading
        left_rows = [row for row in left_rows if not row[0].startswith("12 ")]
    y = 168.0
    for label, amount in left_rows:
        page.put(_LEFT, y, leader(label, amount))
        y += _PITCH
    i1, i3, i8, ifed, istate = spec.interest
    page.put(
        _RIGHT - 2.0,
        150.0,
        "Form 1099 " + spaced("-INT") + f"  {YEAR}  " + spaced("Interest Income"),
    )
    right_rows = [
        ("1 Interest Income", i1),
        ("2 Early Withdrawal Penalty", "0.00"),
        ("3 Int. on U.S. Savings Bonds & Treas.", i3),
        ("4 Federal Income Tax Withheld", ifed),
        ("5 Investment Expenses", "0.00"),
        ("6 Foreign Tax Paid", "0.00"),
        ("8 Tax-Exempt Interest", i8),
        ("9 Specified Private Activity Bond Interest", "0.00"),
        ("15 State", "CA"),
        ("17 State Tax Withheld", istate),
    ]
    y = 168.0
    for label, amount in right_rows:
        page.put(_RIGHT, y, leader(label, amount))
        y += _PITCH
    # two summary blocks side by side
    oid, oid_t = spec.oid
    page.put(_LEFT, 300.0, f"Summary of {YEAR} Original Issue Discount Items")
    page.put(_RIGHT, 300.0, f"Summary of {YEAR} Sales Proceeds")
    for k, (label, amount) in enumerate(
        [
            ("Original Issue Discount", oid),
            ("Market Discount", "0.00"),
            ("Acquisition Premium", "0.00"),
            ("OID on U.S. Treasury Obligations", oid_t),
            ("Tax-Exempt Original Issue Discount", "0.00"),
        ]
    ):
        page.put(_LEFT + 16.0, 312.0 + k * _PITCH, leader(label, amount, 54))
    page.put(_RIGHT + 16.0, 312.0, leader("Sales Proceeds", "99,999.99", 54))
    page.put(_RIGHT + 16.0, 321.0, leader("Federal Income Tax Withheld", "0.00", 54))
    page.put(
        90.0,
        380.0,
        "This is important tax information and is being furnished to the Internal Revenue Service.",
    )
    page.put(
        90.0,
        389.0,
        "If you are required to file a return, a negligence penalty may be imposed on you.",
    )
    page.put(
        90.0,
        398.0,
        "Questions? Call (cid:9)(cid:99)555.555.0100 or visit the Example Securities site.",
    )

    income += [
        ParsedIncomeItem("ordinary_dividends", money(a1), "1099-DIV", "1a", "Ordinary dividends"),
        ParsedIncomeItem(
            "qualified_dividends", money(b1), "1099-DIV", "1b", "Qualified dividends", is_memo=True
        ),
        ParsedIncomeItem(
            "long_term_capital_gain", money(a2), "1099-DIV", "2a", "Capital gain distributions"
        ),
        ParsedIncomeItem(
            "other", money(b12), "1099-DIV", "12", "Exempt-interest dividends", is_memo=True
        ),
        ParsedIncomeItem("interest", money(oid), "1099-OID", "1", "Original issue discount"),
        ParsedIncomeItem(
            "interest", money(oid_t), "1099-OID", "8", "OID on U.S. Treasury obligations"
        ),
        ParsedIncomeItem("interest", money(i1), "1099-INT", "1", "Interest income"),
        ParsedIncomeItem(
            "interest",
            money(i3),
            "1099-INT",
            "3",
            "Interest on U.S. savings bonds and Treasury obligations",
        ),
        ParsedIncomeItem("other", money(i8), "1099-INT", "8", "Tax-exempt interest", is_memo=True),
    ]
    taxes += _withheld("1099-DIV", dfed, "OH", dstate)
    taxes += _withheld("1099-OID", "0.00", "", "0.00")
    taxes += _withheld("1099-INT", ifed, "CA", istate)
    return page, income, taxes


def _withheld(form: str, federal: str, state: str, state_tax: str) -> list[ParsedTaxItem]:
    taxes: list[ParsedTaxItem] = []
    if money(federal) > 0:
        taxes.append(
            ParsedTaxItem(
                "federal_income_tax_withheld",
                money(federal),
                form,
                "4",
                "Federal income tax withheld",
            )
        )
    if money(state_tax) > 0 and state:
        taxes.append(
            ParsedTaxItem(
                "state_income_tax_withheld",
                money(state_tax),
                form,
                "16",
                "State income tax withheld",
                jurisdiction=state,
            )
        )
    return taxes


def _page2(spec: Spec, total: int) -> tuple[_Page, list[ParsedIncomeItem]]:
    page = _Page(2, total)
    page.header()
    page.put(24.0, 76.0, RECIPIENT_NAME)
    page.put(300.0, 82.0, f"{YEAR} TAX REPORTING STATEMENT")
    y = 100.0
    if spec.continued_div:
        page.put(24.0, y, f"Form 1099-DIV {YEAR} Dividends and Distributions (continued)")
        y += 14.0
        page.put(_LEFT, y, leader("12 Exempt-Interest Dividends", spec.div[3]))
        y += 3 * _PITCH
    page.put(
        24.0,
        y,
        "Form " + spaced("1099-MISC") + f"  {YEAR}  " + spaced("Miscellaneous Information"),
    )
    page.put(_RIGHT, y, "Distributions and Charges*")
    roy, other = spec.misc
    left = [
        ("2 Royalties", roy),
        ("3 Other Income", other),
        ("4 Federal Income Tax Withheld", "0.00"),
        ("8 Sub. Payments in Lieu of Div. or Int.", "0.00"),
        ("16 State Tax Withheld", "0.00"),
    ]
    right = [
        ("Limited Partnership Income", "125.00"),
        ("Non-Reportable Dividends and Interest", "0.00"),
        ("Non-Reportable Tax-Exempt Interest", "0.00"),
        ("Taxable Muni Accrued Int. Paid", "0.00"),
        ("Margin Interest", "9.00"),
    ]
    row_y = y + 18.0
    for (label, amount), (label2, amount2) in zip(left, right, strict=True):
        page.put(_LEFT, row_y, leader(label, amount, 56))
        page.put(_RIGHT, row_y, leader(label2, amount2, 56))
        row_y += _PITCH
    # instructions for the recipient: names every form, no amounts
    base = 280.0
    page.put(212.0, base, "INSTRUCTIONS FOR RECIPIENT OF FORMS 1099, COPY B - READ CAREFULLY")
    prose = [
        "The amounts listed on Forms 1099-DIV, 1099-INT, 1099-MISC, 1099-OID and 1099-B represent dividends,",
        "interest, miscellaneous information, original issue discount and/or gross proceeds payments made",
        "1099-DIV - DIVIDENDS AND DISTRIBUTIONS (OMB No. 1545-0110) Line 1a shows total ordinary dividends.",
        "Nominees: if your TIN is shown on this form you are considered a nominee recipient and must file",
        "Form 1099-DIV, 1099-INT, 1099-MISC, 1099-OID or 1099-B, as applicable, with the IRS for each owner.",
        "1099-B - PROCEEDS FROM BROKER AND BARTER EXCHANGE TRANSACTIONS Report on Form 8949 and Schedule D.",
    ]
    for k, line in enumerate(prose):
        page.put(12.0, base + 14.0 + k * 8.0, line)
    income = [
        ParsedIncomeItem("other", money(roy), "1099-MISC", "2", "Royalties"),
        ParsedIncomeItem("other", money(other), "1099-MISC", "3", "Other income"),
    ]
    return page, income


def _lot_block(page: _Page, y: float, lots: tuple[Lot, ...], with_subtotals: bool) -> float:
    """Print `lots` grouped by description, each group followed by a security subtotal."""
    groups: dict[str, list[Lot]] = {}
    for lot in lots:
        groups.setdefault(lot.desc, []).append(lot)
    for desc, group in groups.items():
        page.put(12.0, y, f"{desc} CUSIP Number 99999X999")
        y += 11.0
        for lot in group:
            page.put(42.0, y, lot.row())
            y += 10.0
        if with_subtotals:
            proceeds = sum(lot.proceeds for lot in group)
            cost = sum(lot.cost for lot in group)
            wash = sum(lot.wash for lot in group)
            gain = sum(lot.gain for lot in group)
            page.put(
                96.0,
                y,
                f"Security Subtotal {fmt(proceeds)} {fmt(cost)} 0.00 {fmt(wash)} {fmt(gain)}",
            )
            y += 13.0
    return y


def _totals(lots: tuple[Lot, ...]) -> str:
    proceeds = sum(lot.proceeds for lot in lots)
    cost = sum(lot.cost for lot in lots)
    wash = sum(lot.wash for lot in lots)
    return f"{fmt(proceeds)} {fmt(cost)} 0.00 {fmt(wash)} {fmt(_net(lots))}"


def _b_header(page: _Page, y: float) -> float:
    page.put(
        12.0,
        y,
        "Form " + spaced("1099-B") + f" {YEAR} PROCEEDS FROM BROKER & BARTER EXCHANGE TRANSACTIONS",
    )
    page.put(216.0, y + 15.0, "1b. Date 1c. Date Sold 1f. Accrued 1g. Wash Sale")
    page.put(
        12.0,
        y + 24.0,
        "1a. Description of Property Acquired or Disposed 1d. Proceeds 1e. Cost Basis "
        "Market Discount Loss Disallowed Gain or Loss",
    )
    return y + 50.0


def _b_pages(spec: Spec, total: int) -> list[_Page]:
    detail_only = spec.b_mode == "detail_only"
    category = spec.b_mode in ("full", "category_subtotals")
    full = spec.b_mode == "full"
    pages: list[_Page] = []

    p3 = _Page(3, total)
    p3.header()
    p3.put(24.0, 76.0, RECIPIENT_NAME)
    p3.put(300.0, 82.0, f"{YEAR} TAX REPORTING STATEMENT")
    p3.put(
        12.0,
        110.0,
        "The following sections are provided to facilitate your review and the preparation",
    )
    p3.put(
        12.0,
        119.0,
        'of your tax return. The 1099-B section includes sales of "covered" and "noncovered"',
    )
    p3.put(12.0, 128.0, "securities. Transactions marked N/C have no computed gain or loss.")
    y = _b_header(p3, 160.0)
    p3.put(12.0, y, "SHORT TERM CAPITAL GAINS AND LOSSES - 1099-B Line 2 - Box 2 Net Proceeds")
    y += 14.0
    p3.put(12.0, y, "COVERED TRANSACTIONS - Cost basis reported to IRS - Form 8949, Part I, (A)")
    y = _lot_block(p3, y + 16.0, spec.st_covered, not detail_only)
    if category:
        p3.put(
            12.0,
            y,
            f"Covered Short Term Capital Gains and Losses Subtotal {_totals(spec.st_covered)}",
        )
    pages.append(p3)

    p4 = _Page(4, total)
    p4.header()
    p4.put(24.0, 76.0, RECIPIENT_NAME)
    p4.put(300.0, 82.0, f"{YEAR} TAX REPORTING STATEMENT")
    y = _b_header(p4, 110.0)
    p4.put(
        12.0, y, "NONCOVERED TRANSACTIONS-Cost basis not reported to IRS - Form 8949, Part I, (B)"
    )
    y = _lot_block(p4, y + 16.0, spec.st_noncovered, not detail_only)
    if category:
        p4.put(
            12.0,
            y,
            f"Noncovered Short Term Capital Gains and Losses Subtotal {_totals(spec.st_noncovered)}",
        )
        y += 18.0
    if full:
        st_all = spec.st_covered + spec.st_noncovered
        p4.put(12.0, y, f"NET SHORT TERM CAPITAL GAINS AND LOSSES {_totals(st_all)}")
        y += 20.0
    p4.put(12.0, y, "LONG TERM CAPITAL GAINS AND LOSSES - 1099-B Line 2 - Box 2 Net Proceeds")
    y += 14.0
    p4.put(12.0, y, "COVERED TRANSACTIONS - Cost basis reported to IRS - Form 8949, Part II, (D)")
    y = _lot_block(p4, y + 16.0, spec.lt_covered, not detail_only)
    if category:
        p4.put(
            12.0,
            y,
            f"Covered Long Term Capital Gains and Losses Subtotal {_totals(spec.lt_covered)}",
        )
        y += 18.0
    if full:
        p4.put(12.0, y, f"NET LONG TERM CAPITAL GAINS AND LOSSES {_totals(spec.lt_covered)}")
    pages.append(p4)

    p5 = _Page(5, total)
    p5.header()
    p5.put(24.0, 76.0, RECIPIENT_NAME)
    p5.put(300.0, 82.0, f"{YEAR} TAX REPORTING STATEMENT")
    y = _b_header(p5, 110.0)
    p5.put(12.0, y, "OTHER TRANSACTIONS - Cost basis not reported to IRS - Form 8949, (X)")
    y = _lot_block(p5, y + 16.0, spec.other, not detail_only)
    p5.put(12.0, y, "Other Transactions Subtotal 8,000.00 0.00 0.00 0.00")
    y += 24.0
    everything = spec.st_covered + spec.st_noncovered + spec.lt_covered
    if full:
        p5.put(12.0, y, f"SALES PROCEEDS AND NET GAINS AND LOSSES* {_totals(everything)}")
        y += 18.0
        p5.put(12.0, y, f"COVERED SHORT TERM GAINS/LOSSES {fmt(_net(spec.st_covered))}")
        p5.put(12.0, y + 9.0, f"NONCOVERED SHORT TERM GAINS/LOSSES {fmt(_net(spec.st_noncovered))}")
        p5.put(12.0, y + 18.0, f"COVERED LONG TERM GAINS/LOSSES {fmt(_net(spec.lt_covered))}")
        p5.put(12.0, y + 27.0, "OTHER TRANSACTIONS N/C")
    p5.put(36.0, 440.0, "(W) This transaction has been identified as a Wash Sale. See Form 8949.")
    p5.put(
        33.0,
        450.0,
        "N/C Results may not be calculated for transactions that involve partnership interests.",
    )
    pages.append(p5)
    return pages


def build_statement(spec: Spec) -> tuple[list[list[BoxItem]], ParsedTaxForm]:
    total = 5 if spec.show_b else 2
    page1, income, taxes = _page1(spec, total)
    page2, misc_income = _page2(spec, total)
    income += misc_income
    pages = [page1, page2]
    warnings: list[str] = []
    if spec.show_b:
        pages += _b_pages(spec, total)
        st = _net(spec.st_covered + spec.st_noncovered)
        lt = _net(spec.lt_covered)
        income += [
            ParsedIncomeItem(
                "short_term_capital_gain", st, "1099-B", "ST", "Net short-term gain or loss"
            ),
            ParsedIncomeItem(
                "long_term_capital_gain", lt, "1099-B", "LT", "Net long-term gain or loss"
            ),
        ]
        warnings.append(_NC_WARNING)
    form = ParsedTaxForm(
        form_type="1099-consolidated",
        tax_year=YEAR,
        payer_name=BROKER.name,
        payer_tin_last4=last4(BROKER.ein),
        account_last4=last4(ACCOUNT),
        is_corrected=False,
        filing_status=None,
        agi_minor=None,
        taxable_income_minor=None,
        income=tuple(income),
        taxes=tuple(taxes),
        warnings=tuple(warnings),
    )
    return [p.items for p in pages], form


_ZERO = Spec(
    div=("0.00", "0.00", "0.00", "0.00", "0.00", "0.00"),
    interest=("0.00", "0.00", "0.00", "0.00", "0.00"),
    oid=("0.00", "0.00"),
    show_b=False,
)

_SPECS = {
    "two_up": Spec(),
    "b_category_subtotals": Spec(b_mode="category_subtotals"),
    "b_detail_only": Spec(b_mode="detail_only"),
    "zero_sections": _ZERO,
    "section_continued": Spec(show_b=False, continued_div=True),
}


class Tax1099StatementBuilder:
    layout_id = "tax_1099_broker"
    variants: tuple[str, ...] = tuple(_SPECS)

    def build(self, out_dir: Path, *, variant: str, seed: int) -> dict[str, Any]:
        spec = _SPECS[variant]
        pages, form = build_statement(spec)
        render_boxes_pdf(
            out_dir / f"{self.layout_id}_{variant}.pdf",
            pages,
            page_size=landscape(letter),
        )
        document = ParsedTaxDocument(tax_year=YEAR, forms=(form,))
        return build_golden(document, expected_warnings=list(form.warnings))


BUILDER = Tax1099StatementBuilder()
