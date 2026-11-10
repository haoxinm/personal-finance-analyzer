"""Synthetic Form 1099-DIV fixtures (IRS box layout), layout id ``tax_1099_div``.

Variants: ``normal`` (1a, 1b, 2a, 4), ``qualified_gt_ordinary`` (1b above 1a: warning),
``exempt_interest_memo`` (box 12 memo).

Geometry variants: ``two_payers_side_by_side`` (two brokers' forms scaled left and right on a
landscape page) and ``second_form_blank_box1`` (two stacked forms, box 1a of the second blank:
one form, a warning and an unparsed region). All identities are invented (see `tax_common`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import landscape, letter

from personal_finance_analyzer.core.tax_types import (
    ParsedIncomeItem,
    ParsedTaxDocument,
    ParsedTaxForm,
    ParsedTaxItem,
    UnparsedRegion,
)
from tests.fixtures.gen.irs_boxes import Row, form_items, money_text, render_1099
from tests.fixtures.gen.tax_common import (
    ACCOUNT_NUMBER,
    ACCOUNT_NUMBER_2,
    BROKER,
    BROKER_B,
    BoxItem,
    Entity,
    build_golden,
    last4,
    render_boxes_pdf,
)

_YEAR = 2024
_SRC = "1099-DIV"
_QUALIFIED_WARNING = "Qualified dividends (box 1b) exceed total ordinary dividends (box 1a)."


def _rows(*, a1: int | None, b1: int, a2: int, b4: int, b12: int) -> list[Row]:
    def val(amount: int) -> str:
        return money_text(amount) if amount else ""

    return [
        (None, ("1a Total ordinary dividends", None, "" if a1 is None else money_text(a1)), None),
        (
            None,
            ("1b Qualified dividends", None, val(b1)),
            ("2a Total capital gain distr.", None, val(a2)),
        ),
        (
            None,
            ("2b Unrecap. Sec. 1250 gain", None, ""),
            ("2c Section 1202 gain", None, ""),
        ),
        (
            None,
            ("2d Collectibles (28%) gain", None, ""),
            ("2e Section 897 ordinary dividends", None, ""),
        ),
        (
            None,
            ("2f Section 897 capital gain", None, ""),
            ("3 Nondividend distributions", None, ""),
        ),
        (
            None,
            ("4 Federal income tax withheld", None, val(b4)),
            ("5 Section 199A dividends", None, ""),
        ),
        (None, ("6 Investment expenses", None, ""), ("7 Foreign tax paid", None, "")),
        (
            None,
            ("8 Foreign country or U.S.", "possession", ""),
            ("9 Cash liquidation distributions", None, ""),
        ),
        (
            None,
            ("10 Noncash liquidation", "distributions", ""),
            ("11 FATCA filing requirement", None, "[ ]"),
        ),
        (
            None,
            ("12 Exempt-interest dividends", None, val(b12)),
            ("13 Specified private activity", "bond interest dividends", ""),
        ),
        (
            ("14 State", None, ""),
            ("15 State identification no.", None, ""),
            ("16 State tax withheld", None, ""),
        ),
    ]


_BLANK_WARNING = (
    "A 1099-DIV form was found but box 1a (ordinary dividends) has no value; "
    "that form was skipped. Enter its figures manually."
)
_LAYOUT_VARIANTS = ("two_payers_side_by_side", "second_form_blank_box1")
_SCALE = 0.70


def _expected_form(payer: Entity, account: str, ordinary: int) -> ParsedTaxForm:
    return ParsedTaxForm(
        form_type="1099-div",
        tax_year=_YEAR,
        payer_name=payer.name,
        payer_tin_last4=last4(payer.ein),
        account_last4=last4(account),
        is_corrected=False,
        filing_status=None,
        agi_minor=None,
        taxable_income_minor=None,
        income=(
            ParsedIncomeItem(
                "ordinary_dividends", ordinary, _SRC, "1a", "Total ordinary dividends"
            ),
        ),
        taxes=(),
    )


def _page(
    payer: Entity, account: str, a1: int | None, *, x_off: float, y_off: float, scale: float
) -> list[BoxItem]:
    return form_items(
        heading="Form 1099-DIV",
        subtitle="Dividends and Distributions",
        omb="OMB No. 1545-0110",
        rev="(Rev. January 2024)",
        year_text=str(_YEAR),
        corrected=False,
        payer=payer,
        account_number=account,
        rows=_rows(a1=a1, b1=0, a2=0, b4=0, b12=0),
        x_off=x_off,
        y_off=y_off,
        scale=scale,
    )


def _build_layout_variant(pdf_path: Path, variant: str) -> dict[str, Any]:
    first = (BROKER, ACCOUNT_NUMBER, 250_000)
    second = (BROKER_B, ACCOUNT_NUMBER_2, 91_375)
    if variant == "two_payers_side_by_side":
        items = _page(first[0], first[1], first[2], x_off=0.0, y_off=0.0, scale=_SCALE) + _page(
            second[0], second[1], second[2], x_off=380.0, y_off=0.0, scale=_SCALE
        )
        render_boxes_pdf(pdf_path, [items], font_size=7.0 * _SCALE, page_size=landscape(letter))
        forms = tuple(_expected_form(*spec) for spec in (first, second))
        return build_golden(ParsedTaxDocument(tax_year=_YEAR, forms=forms))
    items = _page(first[0], first[1], first[2], x_off=0.0, y_off=0.0, scale=1.0) + _page(
        second[0], second[1], None, x_off=0.0, y_off=470.0, scale=1.0
    )
    render_boxes_pdf(pdf_path, [items], page_size=(612.0, 940.0))
    document = ParsedTaxDocument(
        tax_year=_YEAR,
        forms=(_expected_form(*first),),
        warnings=(_BLANK_WARNING,),
        unparsed=(UnparsedRegion("1099-DIV", (1,), "unreadable"),),
    )
    return build_golden(document, expected_warnings=[_BLANK_WARNING])


class Form1099DivBuilder:
    layout_id = "tax_1099_div"
    variants: tuple[str, ...] = (
        "normal",
        "qualified_gt_ordinary",
        "exempt_interest_memo",
        *_LAYOUT_VARIANTS,
    )

    def build(self, out_dir: Path, *, variant: str, seed: int) -> dict[str, Any]:
        pdf_path = out_dir / f"{self.layout_id}_{variant}.pdf"
        if variant in _LAYOUT_VARIANTS:
            return _build_layout_variant(pdf_path, variant)
        a1, b1, a2, b4, b12 = 0, 0, 0, 0, 0
        warnings: list[str] = []
        if variant == "normal":
            a1, b1, a2, b4 = 250_000, 180_000, 64_000, 25_000
        elif variant == "qualified_gt_ordinary":
            a1, b1 = 50_000, 80_000
            warnings.append(_QUALIFIED_WARNING)
        elif variant == "exempt_interest_memo":
            a1, b1, b12 = 100_000, 70_000, 30_000
        else:
            raise ValueError(f"unknown variant {variant!r}")

        income = [
            ParsedIncomeItem("ordinary_dividends", a1, _SRC, "1a", "Total ordinary dividends")
        ]
        if b1:
            income.append(
                ParsedIncomeItem(
                    "qualified_dividends", b1, _SRC, "1b", "Qualified dividends", is_memo=True
                )
            )
        if a2:
            income.append(
                ParsedIncomeItem(
                    "long_term_capital_gain", a2, _SRC, "2a", "Total capital gain distributions"
                )
            )
        if b12:
            income.append(
                ParsedIncomeItem(
                    "other", b12, _SRC, "12", "Exempt-interest dividends", is_memo=True
                )
            )
        taxes = (
            [
                ParsedTaxItem(
                    "federal_income_tax_withheld", b4, _SRC, "4", "Federal income tax withheld"
                )
            ]
            if b4
            else []
        )
        form = ParsedTaxForm(
            form_type="1099-div",
            tax_year=_YEAR,
            payer_name=BROKER.name,
            payer_tin_last4=last4(BROKER.ein),
            account_last4=last4(ACCOUNT_NUMBER),
            is_corrected=False,
            filing_status=None,
            agi_minor=None,
            taxable_income_minor=None,
            income=tuple(income),
            taxes=tuple(taxes),
            warnings=tuple(warnings),
        )
        render_1099(
            pdf_path,
            heading="Form 1099-DIV",
            subtitle="Dividends and Distributions",
            omb="OMB No. 1545-0110",
            rev="(Rev. January 2024)",
            year_text=str(_YEAR),
            corrected=False,
            payer=BROKER,
            account_number=ACCOUNT_NUMBER,
            rows=_rows(a1=a1, b1=b1, a2=a2, b4=b4, b12=b12),
        )
        return build_golden(
            ParsedTaxDocument(tax_year=_YEAR, forms=(form,)), expected_warnings=warnings
        )


BUILDER = Form1099DivBuilder()
