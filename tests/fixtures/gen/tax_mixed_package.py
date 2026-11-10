"""Synthetic mixed tax packages: different form types in ONE PDF (layout id
``tax_mixed_package``).

Real packages put a W-2, interest and dividend forms (even a recipient's instruction page) in a
single file. Variants: ``w2_int_div`` (a W-2 page, a 1099-INT page and a 1099-DIV page),
``w2_and_int_side_by_side`` (a W-2 and a 1099-INT scaled onto one landscape page) and
``with_instruction_page`` (as ``w2_int_div`` plus a page of instructions that names every form
but carries no amount: it must not create a form). All identities are invented (see
`tax_common`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import landscape, letter

from personal_finance_analyzer.core.tax_types import ParsedTaxDocument, ParsedTaxForm
from tests.fixtures.gen import tax_1099_div as div
from tests.fixtures.gen import tax_1099_int as interest
from tests.fixtures.gen import tax_w2 as w2
from tests.fixtures.gen.tax_common import (
    ACCOUNT_NUMBER,
    ACCOUNT_NUMBER_2,
    BANK,
    BROKER,
    BoxItem,
    build_golden,
    render_boxes_pdf,
)

_SCALE = 0.70
_RIGHT_X = 380.0
_INT_CENTS = 123_456
_DIV_CENTS = 250_000

_INSTRUCTIONS = (
    "INSTRUCTIONS FOR RECIPIENT OF FORMS W-2, 1099-INT AND 1099-DIV",
    "The amounts listed on Forms 1099-DIV, 1099-INT, 1099-MISC, 1099-OID and 1099-B represent income",
    "paid or credited to your account during the calendar year. Form W-2 shows wages and withholding.",
    "1099-INT - INTEREST INCOME (OMB No. 1545-0112) Box 1 shows taxable interest paid to you.",
    "Form 1099-DIV, Form 1099-INT and Form W-2 must be reported on your return as described below.",
    "If you are a nominee recipient you must file Form 1099-INT or 1099-DIV for each other owner.",
)


def _w2_page(x_off: float = 0.0, scale: float = 1.0) -> list[BoxItem]:
    return w2._w2_items(30.0, w2._with(w2._NORMAL, local=None), x_off=x_off, scale=scale)


def _int_page(x_off: float = 0.0, scale: float = 1.0) -> list[BoxItem]:
    return interest._page(BANK, ACCOUNT_NUMBER, _INT_CENTS, x_off=x_off, y_off=0.0, scale=scale)


def _div_page() -> list[BoxItem]:
    return div._page(BROKER, ACCOUNT_NUMBER_2, _DIV_CENTS, x_off=0.0, y_off=0.0, scale=1.0)


def _expected() -> dict[str, ParsedTaxForm]:
    int_form: ParsedTaxForm = interest._form(
        BANK, ACCOUNT_NUMBER, [interest._int_item("1", "Interest income", _INT_CENTS)]
    )
    return {
        "w2": w2._expected_form(w2._with(w2._NORMAL, local=None)),
        "int": int_form,
        "div": div._expected_form(BROKER, ACCOUNT_NUMBER_2, _DIV_CENTS),
    }


class MixedPackageBuilder:
    layout_id = "tax_mixed_package"
    variants: tuple[str, ...] = ("w2_int_div", "w2_and_int_side_by_side", "with_instruction_page")

    def build(self, out_dir: Path, *, variant: str, seed: int) -> dict[str, Any]:
        path = out_dir / f"{self.layout_id}_{variant}.pdf"
        forms = _expected()
        if variant == "w2_and_int_side_by_side":
            page = _w2_page(scale=_SCALE) + _int_page(x_off=_RIGHT_X, scale=_SCALE)
            render_boxes_pdf(path, [page], font_size=7.0 * _SCALE, page_size=landscape(letter))
            expected = [forms["w2"], forms["int"]]
        else:
            pages = [_w2_page(), _int_page(), _div_page()]
            if variant == "with_instruction_page":
                pages.append(
                    [(36.0, 60.0 + 12.0 * k, line) for k, line in enumerate(_INSTRUCTIONS)]
                )
            elif variant != "w2_int_div":
                raise ValueError(f"unknown variant {variant!r}")
            render_boxes_pdf(path, pages)
            expected = [forms["w2"], forms["int"], forms["div"]]
        return build_golden(ParsedTaxDocument(tax_year=2024, forms=tuple(expected)))


BUILDER = MixedPackageBuilder()
