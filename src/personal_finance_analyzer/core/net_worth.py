"""Net-worth domain vocabulary: balance categories and the sign convention.

Pure data and helpers: no I/O, no database, no network, and no LLM. Balances are local-only
financial data and never reach an egress seam.

A balance is a non-negative integer count of minor currency units. Asset categories (checking,
savings, investment) count positively toward net worth; a liability (credit card) stores the
*amount owed* as a magnitude and counts negatively.
"""

from __future__ import annotations

from typing import Literal, get_args

BalanceCategory = Literal["checking", "savings", "investment", "credit_card"]
BALANCE_CATEGORIES: tuple[str, ...] = get_args(BalanceCategory)
ASSET_CATEGORIES: tuple[str, ...] = ("checking", "savings", "investment")
LIABILITY_CATEGORIES: tuple[str, ...] = ("credit_card",)


def signed_balance(category: str, balance_minor: int) -> int:
    """`+balance` for assets, `-balance` for liabilities."""
    if category in ASSET_CATEGORIES:
        return balance_minor
    if category in LIABILITY_CATEGORIES:
        return -balance_minor
    raise ValueError(f"unknown balance category: {category!r}")


def normalize_account_name(raw: str) -> str:
    """Strip and collapse internal whitespace; reject an empty result."""
    name = " ".join(raw.split())
    if not name:
        raise ValueError("account name must not be empty")
    return name
