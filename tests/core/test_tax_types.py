"""Vocabulary and shape checks for `core/tax_types.py`."""

from __future__ import annotations

import dataclasses
import re
from typing import get_args

import pytest

from personal_finance_analyzer.core import tax_types as tt


def test_form_role_covers_every_form_type() -> None:
    assert set(tt.FORM_ROLE) == set(get_args(tt.FormType))
    assert {f for f, r in tt.FORM_ROLE.items() if r == "return"} == {"1040", "state-return"}


def test_tax_groups_partition_every_category_exactly_once() -> None:
    flat = [c for group in tt.TAX_GROUPS.values() for c in group]
    assert sorted(flat) == sorted(get_args(tt.TaxCategory))
    assert len(flat) == len(set(flat))
    assert get_args(tt.TaxCategory) == tt.TAX_CATEGORIES


def test_income_categories_single_source_of_truth() -> None:
    assert get_args(tt.IncomeCategory) == tt.INCOME_CATEGORIES
    assert set(tt.INCOME_CATEGORIES) >= tt.MEMO_INCOME_CATEGORIES
    assert set(tt.INCOME_CATEGORIES) >= tt.SIGNED_INCOME_CATEGORIES


def test_labels_cover_every_category() -> None:
    assert set(tt.INCOME_CATEGORY_LABELS) == set(tt.INCOME_CATEGORIES)
    assert set(tt.TAX_CATEGORY_LABELS) == set(tt.TAX_CATEGORIES)
    assert all(tt.INCOME_CATEGORY_LABELS.values())
    assert all(tt.TAX_CATEGORY_LABELS.values())


def _form() -> tt.ParsedTaxForm:
    return tt.ParsedTaxForm(
        form_type="w2",
        tax_year=2024,
        payer_name="Invented Corp",
        payer_tin_last4="1234",
        account_last4=None,
        is_corrected=False,
        filing_status=None,
        agi_minor=None,
        taxable_income_minor=None,
        income=(tt.ParsedIncomeItem("salary_wages", 5_000_000, "W-2", "1", "Wages", False),),
        taxes=(tt.ParsedTaxItem("federal_income_tax_withheld", 600_000, "W-2", "2", "Federal"),),
    )


@pytest.mark.parametrize(
    "obj",
    [
        tt.ParsedIncomeItem("interest", 1, "1099-INT", "1", None),
        tt.ParsedTaxItem("refund", 1, "1040", None, None),
        _form(),
        tt.ParsedTaxDocument(tax_year=2024, forms=(_form(),)),
    ],
)
def test_dataclasses_frozen_and_slotted(obj: object) -> None:
    assert not hasattr(obj, "__dict__")
    field = dataclasses.fields(obj)[0].name  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(obj, field, None)


def test_parsed_form_repr_has_no_ssn_shaped_value() -> None:
    text = repr(_form())
    assert not re.search(r"\d{3}-?\d{2}-?\d{4}", text)
    assert not re.search(r"\d{9}", text)


def test_tax_parser_protocol_is_not_a_statement_parser() -> None:
    from personal_finance_analyzer.core.types import StatementParser

    class Fake:
        id = "tax_fake"
        version = "1"
        form_types = ("w2",)

        def detect(self, doc: object) -> float:
            return 0.0

        def parse(self, doc: object, *, tax_year: int | None = None) -> tt.ParsedTaxDocument:
            raise NotImplementedError

    assert isinstance(Fake(), tt.TaxFormParser)
    assert not isinstance(Fake(), StatementParser)
