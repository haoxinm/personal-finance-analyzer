from __future__ import annotations

import pytest

from personal_finance_analyzer.core.net_worth import (
    BALANCE_CATEGORIES,
    normalize_account_name,
    signed_balance,
)


def test_categories() -> None:
    assert BALANCE_CATEGORIES == ("checking", "savings", "investment", "credit_card")


@pytest.mark.parametrize(
    ("category", "expected"),
    [("checking", 500), ("savings", 500), ("investment", 500), ("credit_card", -500)],
)
def test_signed_balance(category: str, expected: int) -> None:
    assert signed_balance(category, 500) == expected


def test_signed_balance_unknown() -> None:
    with pytest.raises(ValueError):
        signed_balance("cash", 1)


def test_normalize_account_name() -> None:
    assert normalize_account_name("  Chase   main \t checking ") == "Chase main checking"
    with pytest.raises(ValueError):
        normalize_account_name("   ")
