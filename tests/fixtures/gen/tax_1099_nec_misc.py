"""Synthetic Form 1099-NEC / 1099-MISC fixtures (IRS box layout), layout id ``tax_1099_nec_misc``.

Variants: ``nec_normal`` (box 1 only), ``misc_rents_royalties`` (boxes 1-3), and
``nec_with_withholding_state`` (box 1, federal box 4, state boxes 5-7). All identities are invented
(see `tax_common`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from personal_finance_analyzer.core.tax_types import (
    FormType,
    ParsedIncomeItem,
    ParsedTaxDocument,
    ParsedTaxForm,
    ParsedTaxItem,
)
from tests.fixtures.gen.irs_boxes import Row, money_text, render_1099
from tests.fixtures.gen.tax_common import (
    ACCOUNT_NUMBER,
    EMPLOYER_A,
    EMPLOYER_B,
    build_golden,
    last4,
)

_YEAR = 2024


def _val(amount: int) -> str:
    return money_text(amount) if amount else ""


def _nec_rows(*, box1: int, box4: int, state: tuple[str, str, int] | None) -> list[Row]:
    return [
        (None, ("1 Nonemployee compensation", None, money_text(box1)), None),
        (None, ("2 Payer made direct sales", "of consumer products", "[ ]"), None),
        (None, ("4 Federal income tax withheld", None, _val(box4)), None),
        (
            None,
            ("5 State tax withheld", None, money_text(state[2]) if state else ""),
            ("6 State/Payer's state no.", None, f"{state[0]} {state[1]}" if state else ""),
        ),
        (None, ("7 State income", None, money_text(box1) if state else ""), None),
    ]


def _misc_rows(*, rents: int, royalties: int, other: int) -> list[Row]:
    return [
        (None, ("1 Rents", None, _val(rents)), None),
        (None, ("2 Royalties", None, _val(royalties)), ("3 Other income", None, _val(other))),
        (
            None,
            ("4 Federal income tax withheld", None, ""),
            ("5 Fishing boat proceeds", None, ""),
        ),
        (
            None,
            ("6 Medical and health care", "payments", ""),
            ("7 Payer made direct sales", "of consumer products", "[ ]"),
        ),
        (
            None,
            ("8 Substitute payments in lieu", "of dividends or interest", ""),
            ("9 Crop insurance proceeds", None, ""),
        ),
        (
            None,
            ("10 Gross proceeds paid to", "an attorney", ""),
            ("11 Fish purchased for resale", None, ""),
        ),
        (
            None,
            ("12 Section 409A deferrals", None, ""),
            ("13 FATCA filing requirement", None, "[ ]"),
        ),
        (
            None,
            ("14 Excess golden parachute", "payments", ""),
            ("15 Nonqualified deferred", "compensation", ""),
        ),
        (
            ("16 State tax withheld", None, ""),
            ("17 State/Payer's state no.", None, ""),
            ("18 State income", None, ""),
        ),
    ]


class Form1099NecMiscBuilder:
    layout_id = "tax_1099_nec_misc"
    variants: tuple[str, ...] = ("nec_normal", "misc_rents_royalties", "nec_with_withholding_state")

    def build(self, out_dir: Path, *, variant: str, seed: int) -> dict[str, Any]:
        pdf_path = out_dir / f"{self.layout_id}_{variant}.pdf"
        form_type: FormType
        income: list[ParsedIncomeItem]
        taxes: list[ParsedTaxItem] = []
        if variant in ("nec_normal", "nec_with_withholding_state"):
            form_type, source = "1099-nec", "1099-NEC"
            with_state = variant == "nec_with_withholding_state"
            box1 = 1_850_000 if with_state else 925_000
            box4 = 370_000 if with_state else 0
            state = ("CA", "5551234", 92_500) if with_state else None
            income = [
                ParsedIncomeItem("self_employment", box1, source, "1", "Nonemployee compensation")
            ]
            if box4:
                taxes.append(
                    ParsedTaxItem(
                        "federal_income_tax_withheld",
                        box4,
                        source,
                        "4",
                        "Federal income tax withheld",
                    )
                )
            if state:
                taxes.append(
                    ParsedTaxItem(
                        "state_income_tax_withheld",
                        state[2],
                        source,
                        "5",
                        "State tax withheld",
                        jurisdiction="CA",
                    )
                )
            payer, account = EMPLOYER_B, None
            heading, subtitle, omb = (
                "Form 1099-NEC",
                "Nonemployee Compensation",
                "OMB No. 1545-0116",
            )
            rows = _nec_rows(box1=box1, box4=box4, state=state)
        elif variant == "misc_rents_royalties":
            form_type, source = "1099-misc", "1099-MISC"
            rents, royalties, other = 1_200_000, 345_075, 80_000
            income = [
                ParsedIncomeItem("other", rents, source, "1", "Rents"),
                ParsedIncomeItem("other", royalties, source, "2", "Royalties"),
                ParsedIncomeItem("other", other, source, "3", "Other income"),
            ]
            payer, account = EMPLOYER_A, ACCOUNT_NUMBER
            heading, subtitle, omb = (
                "Form 1099-MISC",
                "Miscellaneous Information",
                "OMB No. 1545-0115",
            )
            rows = _misc_rows(rents=rents, royalties=royalties, other=other)
        else:
            raise ValueError(f"unknown variant {variant!r}")
        form = ParsedTaxForm(
            form_type=form_type,
            tax_year=_YEAR,
            payer_name=payer.name,
            payer_tin_last4=last4(payer.ein),
            account_last4=last4(account) if account else None,
            is_corrected=False,
            filing_status=None,
            agi_minor=None,
            taxable_income_minor=None,
            income=tuple(income),
            taxes=tuple(taxes),
        )
        render_1099(
            pdf_path,
            heading=heading,
            subtitle=subtitle,
            omb=omb,
            rev="(Rev. January 2024)",
            year_text=str(_YEAR),
            corrected=False,
            payer=payer,
            account_number=account,
            rows=rows,
        )
        return build_golden(ParsedTaxDocument(tax_year=_YEAR, forms=(form,)))


BUILDER = Form1099NecMiscBuilder()
