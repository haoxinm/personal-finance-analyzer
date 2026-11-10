"""Synthetic Form 1099-INT fixtures (IRS box layout), layout id ``tax_1099_int``.

Variants: ``normal`` (boxes 1-3), ``with_withholding_and_state`` (boxes 1, 4 and state 17),
``tax_exempt_memo`` (box 8 memo), ``zero_interest`` (every box printed as 0.00).

Geometry variants: ``two_payers_stacked`` (two payers' forms one above the other on a tall page),
``two_payers_side_by_side`` (scaled forms left and right on a landscape page), ``continued_page``
(the state lines continue on page 2 under ``Form 1099-INT (continued)``: ONE form with the state
item) and ``second_form_blank_box1`` (two stacked forms, the second with box 1 blank: partial
success, one form plus a warning and an unparsed region). All identities are invented (see
`tax_common`).
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
from tests.fixtures.gen.irs_boxes import (
    ROW_Y,
    X0,
    X1,
    X2,
    Cell,
    Row,
    _cell,
    form_items,
    money_text,
    render_1099,
)
from tests.fixtures.gen.tax_common import (
    ACCOUNT_NUMBER,
    ACCOUNT_NUMBER_2,
    BANK,
    CREDIT_UNION,
    BoxItem,
    Entity,
    build_golden,
    last4,
    render_boxes_pdf,
    text_item,
)

_YEAR = 2024
_SRC = "1099-INT"


def _int_item(
    box: str, label: str, amount: int, *, category: Any = "interest", memo: bool = False
) -> ParsedIncomeItem:
    return ParsedIncomeItem(
        category=category, amount_minor=amount, source_form=_SRC, box=box, label=label, is_memo=memo
    )


def _rows(
    *,
    b1: int | None,
    b2: int,
    b3: int,
    b4: int,
    b8: int,
    state: tuple[str, str, int] | None,
    show_zero: bool,
) -> list[Row]:
    def val(amount: int) -> str:
        return money_text(amount) if amount or show_zero else ""

    state_cells: tuple[Cell | None, Cell | None, Cell | None] = (
        ("15 State", None, state[0] if state else ""),
        ("16 State identification no.", None, state[1] if state else ""),
        ("17 State tax withheld", None, money_text(state[2]) if state else ""),
    )
    return [
        (None, ("1 Interest income", None, "" if b1 is None else money_text(b1)), None),
        (
            None,
            ("2 Early withdrawal penalty", None, val(b2)),
            ("3 Interest on U.S. Savings Bonds", "and Treas. obligations", val(b3)),
        ),
        (
            None,
            ("4 Federal income tax withheld", None, val(b4)),
            ("5 Investment expenses", None, ""),
        ),
        (
            None,
            ("6 Foreign tax paid", None, ""),
            ("7 Foreign country or U.S. possession", None, ""),
        ),
        (
            None,
            ("8 Tax-exempt interest", None, val(b8)),
            ("9 Specified private activity", "bond interest", ""),
        ),
        (None, ("10 Market discount", None, ""), ("11 Bond premium", None, "")),
        (
            None,
            ("12 Bond premium on Treasury", "obligations", ""),
            ("13 Bond premium on", "tax-exempt bond", ""),
        ),
        (None, None, ("14 Tax-exempt and tax credit", "bond CUSIP no.", "")),
        state_cells,
    ]


def _expected(income: list[ParsedIncomeItem], taxes: list[ParsedTaxItem]) -> ParsedTaxDocument:
    form = ParsedTaxForm(
        form_type="1099-int",
        tax_year=_YEAR,
        payer_name=BANK.name,
        payer_tin_last4=last4(BANK.ein),
        account_last4=last4(ACCOUNT_NUMBER),
        is_corrected=False,
        filing_status=None,
        agi_minor=None,
        taxable_income_minor=None,
        income=tuple(income),
        taxes=tuple(taxes),
    )
    return ParsedTaxDocument(tax_year=_YEAR, forms=(form,))


_BLANK_BOX1_WARNING = (
    "A 1099-INT form was found but box 1 (interest income) has no value; "
    "that form was skipped. Enter its figures manually."
)
_SCALE = 0.70
_RIGHT_X = 380.0
_STACK_DY = 410.0
_TALL = (612.0, 860.0)
_LAYOUT_VARIANTS = (
    "two_payers_stacked",
    "two_payers_side_by_side",
    "continued_page",
    "second_form_blank_box1",
)


def _form(payer: Entity, account: str, income: list[ParsedIncomeItem], taxes: Any = ()) -> Any:
    return ParsedTaxForm(
        form_type="1099-int",
        tax_year=_YEAR,
        payer_name=payer.name,
        payer_tin_last4=last4(payer.ein),
        account_last4=last4(account),
        is_corrected=False,
        filing_status=None,
        agi_minor=None,
        taxable_income_minor=None,
        income=tuple(income),
        taxes=tuple(taxes),
    )


def _page(
    payer: Entity, account: str, b1: int | None, *, x_off: float, y_off: float, scale: float
) -> list[BoxItem]:
    return form_items(
        heading="Form 1099-INT",
        subtitle="Interest Income",
        omb="OMB No. 1545-0112",
        rev="(Rev. January 2024)",
        year_text=str(_YEAR),
        corrected=False,
        payer=payer,
        account_number=account,
        rows=_rows(b1=b1, b2=0, b3=0, b4=0, b8=0, state=None, show_zero=False),
        x_off=x_off,
        y_off=y_off,
        scale=scale,
    )


def _build_layout_variant(pdf_path: Path, variant: str) -> dict[str, Any]:
    first = (BANK, ACCOUNT_NUMBER, 123_456)
    second = (CREDIT_UNION, ACCOUNT_NUMBER_2, 64_200)
    income = lambda cents: [_int_item("1", "Interest income", cents)]  # noqa: E731
    if variant == "two_payers_stacked":
        items = _page(*first, x_off=0.0, y_off=0.0, scale=1.0) + _page(
            *second, x_off=0.0, y_off=_STACK_DY, scale=1.0
        )
        render_boxes_pdf(pdf_path, [items], page_size=_TALL)
    elif variant == "two_payers_side_by_side":
        items = _page(*first, x_off=0.0, y_off=0.0, scale=_SCALE) + _page(
            *second, x_off=_RIGHT_X, y_off=0.0, scale=_SCALE
        )
        render_boxes_pdf(pdf_path, [items], font_size=7.0 * _SCALE, page_size=landscape(letter))
    elif variant == "continued_page":
        main = form_items(
            heading="Form 1099-INT",
            subtitle="Interest Income",
            omb="OMB No. 1545-0112",
            rev="(Rev. January 2024)",
            year_text=str(_YEAR),
            corrected=False,
            payer=BANK,
            account_number=ACCOUNT_NUMBER,
            rows=_rows(b1=348_000, b2=0, b3=0, b4=0, b8=0, state=None, show_zero=False)[:-1],
        )
        cont: list[BoxItem] = text_item(X2, 44.0, "Form 1099-INT (continued)")
        cont += text_item(X0, 44.0, "Page 2 of 2")
        for x, width, spec in (
            (X0, 210.0, ("15 State", None, "CA")),
            (X1, 142.0, ("16 State identification no.", None, "5551234")),
            (X2, 156.0, ("17 State tax withheld", None, money_text(12_000))),
        ):
            cont += _cell(x, ROW_Y[2], width, spec, 10.0)
        render_boxes_pdf(pdf_path, [main, cont])
        tax = ParsedTaxItem(
            "state_income_tax_withheld",
            12_000,
            _SRC,
            "17",
            "State tax withheld",
            jurisdiction="CA",
        )
        form = _form(BANK, ACCOUNT_NUMBER, income(348_000), [tax])
        return build_golden(ParsedTaxDocument(tax_year=_YEAR, forms=(form,)))
    elif variant == "second_form_blank_box1":
        items = _page(*first, x_off=0.0, y_off=0.0, scale=1.0) + _page(
            CREDIT_UNION, ACCOUNT_NUMBER_2, None, x_off=0.0, y_off=_STACK_DY, scale=1.0
        )
        render_boxes_pdf(pdf_path, [items], page_size=_TALL)
        document = ParsedTaxDocument(
            tax_year=_YEAR,
            forms=(_form(first[0], first[1], income(first[2])),),
            warnings=(_BLANK_BOX1_WARNING,),
            unparsed=(UnparsedRegion("1099-INT", (1,), "unreadable"),),
        )
        return build_golden(document, expected_warnings=[_BLANK_BOX1_WARNING])
    else:
        raise ValueError(variant)
    forms = tuple(_form(p, a, income(c)) for p, a, c in (first, second))
    return build_golden(ParsedTaxDocument(tax_year=_YEAR, forms=forms))


class Form1099IntBuilder:
    layout_id = "tax_1099_int"
    variants: tuple[str, ...] = (
        "normal",
        "with_withholding_and_state",
        "tax_exempt_memo",
        "zero_interest",
        *_LAYOUT_VARIANTS,
    )

    def build(self, out_dir: Path, *, variant: str, seed: int) -> dict[str, Any]:
        pdf_path = out_dir / f"{self.layout_id}_{variant}.pdf"
        if variant in _LAYOUT_VARIANTS:
            return _build_layout_variant(pdf_path, variant)
        income: list[ParsedIncomeItem]
        taxes: list[ParsedTaxItem] = []
        b2 = b3 = b4 = b8 = 0
        state: tuple[str, str, int] | None = None
        show_zero = False
        if variant == "normal":
            b1, b2, b3 = 123_456, 1_200, 24_580
            income = [
                _int_item("1", "Interest income", b1),
                _int_item("3", "Interest on U.S. Savings Bonds and Treasury obligations", b3),
            ]
        elif variant == "with_withholding_and_state":
            b1, b4 = 348_000, 87_000
            state = ("CA", "5551234", 12_000)
            income = [_int_item("1", "Interest income", b1)]
            taxes = [
                ParsedTaxItem(
                    "federal_income_tax_withheld", b4, _SRC, "4", "Federal income tax withheld"
                ),
                ParsedTaxItem(
                    "state_income_tax_withheld",
                    12_000,
                    _SRC,
                    "17",
                    "State tax withheld",
                    jurisdiction="CA",
                ),
            ]
        elif variant == "tax_exempt_memo":
            b1, b8 = 120_000, 64_000
            income = [
                _int_item("1", "Interest income", b1),
                _int_item("8", "Tax-exempt interest", b8, category="other", memo=True),
            ]
        elif variant == "zero_interest":
            b1 = 0
            show_zero = True
            income = [_int_item("1", "Interest income", 0)]
        else:
            raise ValueError(f"unknown variant {variant!r}")
        render_1099(
            pdf_path,
            heading="Form 1099-INT",
            subtitle="Interest Income",
            omb="OMB No. 1545-0112",
            rev="(Rev. January 2024)",
            year_text=str(_YEAR),
            corrected=False,
            payer=BANK,
            account_number=ACCOUNT_NUMBER,
            rows=_rows(b1=b1, b2=b2, b3=b3, b4=b4, b8=b8, state=state, show_zero=show_zero),
        )
        return build_golden(_expected(income, taxes))


BUILDER = Form1099IntBuilder()
