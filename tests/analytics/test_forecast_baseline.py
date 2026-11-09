"""Forecast history and defaults (`analytics/forecast_baseline.py`)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from spend_analyzer.analytics.forecast_baseline import cagr_pct, run_forecast_baseline
from spend_analyzer.db.models import NetWorthHistory
from tests.forecast_seed import SpendSeed, add_balance, add_user, add_w2

# 20.15% all-in on 80,000.00: federal 10,000 + Social Security 4,960 + Medicare 1,160.
_TAXES_2022 = {
    "federal_income_tax_withheld": 1_000_000,
    "social_security_tax": 496_000,
    "medicare_tax": 116_000,
}


def _seed(session: Session) -> int:
    uid = add_user(session, "Alex")
    add_balance(session, uid, "checking", "Chk", 2023, 6, 100_000)
    add_balance(session, uid, "checking", "Chk", 2024, 3, 150_000)
    add_balance(session, uid, "checking", "Chk", 2024, 12, 200_000)
    add_balance(session, uid, "credit_card", "Visa", 2024, 12, 20_000)
    session.add_all(
        [
            NetWorthHistory(user_id=uid, year=2021, amount_minor=50_000, note="from memory"),
            NetWorthHistory(user_id=uid, year=2023, amount_minor=999),  # entries win
        ]
    )
    add_w2(session, uid, 2022, 8_000_000, _TAXES_2022)
    add_w2(
        session,
        uid,
        2023,
        8_800_000,
        {"federal_income_tax_withheld": 1_100_000, "medicare_tax": 700_000},
    )
    card = SpendSeed(session, uid)
    for month in range(1, 13):  # a complete 2023: 12 x 1,000.00
        card.purchase(date(2023, month, 5), 100_000)
    card.purchase(date(2023, 12, 20), -20_000, kind="refund")  # netted: 2023 total 11,800.00
    for month in (1, 2, 3):  # a partial 2024
        card.purchase(date(2024, month, 5), 150_000)
    session.flush()
    return uid


def test_empty_database(session: Session) -> None:
    result = run_forecast_baseline(session, None)
    assert result.history == [] and result.currency == "USD"
    d = result.defaults
    assert d.net_worth_minor is None and d.annual_income_minor is None
    assert d.annual_spending_minor is None and d.income_growth_pct is None
    assert d.spending_annualized is False


def test_history_rows_join_all_sources(session: Session) -> None:
    uid = _seed(session)
    rows = {r.year: r for r in run_forecast_baseline(session, uid).history}
    assert sorted(rows) == [2021, 2022, 2023, 2024]

    manual_only = rows[2021]
    assert (manual_only.net_worth_minor, manual_only.source) == (50_000, "manual")
    assert manual_only.manual_note == "from memory" and manual_only.as_of is None

    # Entries take precedence over the manual value, which stays visible beside it.
    carried = rows[2023]
    assert (carried.net_worth_minor, carried.source, carried.as_of) == (
        100_000,
        "entries",
        "2023-12",
    )
    assert carried.manual_net_worth_minor == 999

    latest = rows[2024]  # 200,000 checking - 20,000 card
    assert (latest.net_worth_minor, latest.as_of) == (180_000, "2024-12")

    no_net_worth = rows[2022]
    assert no_net_worth.net_worth_minor is None and no_net_worth.source is None
    assert no_net_worth.income_minor == 8_000_000
    assert no_net_worth.tax_minor == 1_612_000 and no_net_worth.income_basis == "source_documents"


def test_spending_months_and_savings(session: Session) -> None:
    uid = _seed(session)
    rows = {r.year: r for r in run_forecast_baseline(session, uid).history}
    full, partial = rows[2023], rows[2024]
    assert (full.spending_minor, full.spending_months) == (1_180_000, 12)
    assert (partial.spending_minor, partial.spending_months) == (450_000, 3)
    # Tax is rebuilt from the summary's 4-dp all-in rate (0.2045 x 8,800,000 = 1,799,600).
    assert full.tax_minor == 1_799_600
    assert full.savings_minor == 8_800_000 - 1_799_600 - 1_180_000
    assert partial.savings_minor is None  # a partial spending year has no savings figure
    assert rows[2022].savings_minor is None  # tax year without spending


def test_defaults_derive_from_latest_years(session: Session) -> None:
    uid = _seed(session)
    d = run_forecast_baseline(session, uid).defaults
    assert (d.net_worth_minor, d.net_worth_year) == (180_000, 2024)
    assert (d.annual_income_minor, d.income_year) == (8_800_000, 2023)
    assert d.effective_tax_rate_pct == pytest.approx(20.45)
    # 2024 has only 3 months: annualised from them, and flagged.
    assert (d.annual_spending_minor, d.spending_year, d.spending_annualized) == (
        1_800_000,
        2024,
        True,
    )
    assert d.income_growth_pct == 10.0  # 8,000,000 -> 8,800,000 over one year
    assert d.spending_growth_pct is None  # only one complete spending year
    assert d.savings_rate_pct == pytest.approx(66.14, abs=0.01)


def test_all_users_sums_entries_and_manual_values(session: Session) -> None:
    alex = add_user(session, "Alex")
    sam = add_user(session, "Sam")
    add_balance(session, alex, "checking", "A", 2024, 12, 100_000)
    add_balance(session, sam, "savings", "S", 2024, 12, 40_000)
    session.add_all(
        [
            NetWorthHistory(user_id=alex, year=2020, amount_minor=10_000),
            NetWorthHistory(user_id=sam, year=2020, amount_minor=-3_000),
        ]
    )
    session.flush()
    everyone = {r.year: r for r in run_forecast_baseline(session, None).history}
    assert everyone[2024].net_worth_minor == 140_000
    assert (everyone[2020].net_worth_minor, everyone[2020].manual_note) == (7_000, None)
    sam_only = {r.year: r for r in run_forecast_baseline(session, sam).history}
    assert sam_only[2024].net_worth_minor == 40_000 and sam_only[2020].net_worth_minor == -3_000


def test_users_are_isolated(session: Session) -> None:
    alex = _seed(session)
    sam = add_user(session, "Sam")
    assert run_forecast_baseline(session, sam).history == []
    assert run_forecast_baseline(session, alex).history != []


def test_negative_manual_net_worth_is_kept(session: Session) -> None:
    uid = add_user(session, "Alex")
    session.add(NetWorthHistory(user_id=uid, year=2019, amount_minor=-12_345))
    session.flush()
    d = run_forecast_baseline(session, uid).defaults
    assert d.net_worth_minor == -12_345 and d.net_worth_year == 2019


def test_all_users_picks_entries_or_manual_per_person(session: Session) -> None:
    alex = add_user(session, "Alex")
    sam = add_user(session, "Sam")
    add_balance(session, alex, "checking", "A", 2024, 12, 100_000)  # Alex: entries for 2024
    session.add_all(
        [
            NetWorthHistory(user_id=alex, year=2024, amount_minor=9_999),  # ignored: entries win
            NetWorthHistory(user_id=sam, year=2024, amount_minor=5_000_000),  # Sam: manual only
            NetWorthHistory(user_id=sam, year=2020, amount_minor=1_000),
        ]
    )
    session.flush()

    everyone = {r.year: r for r in run_forecast_baseline(session, None).history}
    row = everyone[2024]
    # Not 100,000 (Alex's entries hiding Sam's manual value): both people count.
    assert row.net_worth_minor == 100_000 + 5_000_000
    assert row.source == "mixed" and row.as_of == "2024-12"
    assert row.manual_net_worth_minor == 9_999 + 5_000_000  # manual values are still reported
    assert everyone[2020].source == "manual" and everyone[2020].net_worth_minor == 1_000
    d = run_forecast_baseline(session, None).defaults
    assert (d.net_worth_minor, d.net_worth_year) == (5_100_000, 2024)

    # One person alone is unchanged.
    assert run_forecast_baseline(session, alex).history[-1].source == "entries"
    assert run_forecast_baseline(session, sam).history[-1].source == "manual"


def test_all_users_carries_forward_a_person_whose_entries_stop(session: Session) -> None:
    alex = add_user(session, "Alex")
    sam = add_user(session, "Sam")
    add_balance(session, alex, "checking", "A", 2023, 6, 100_000)  # stops in 2023
    add_balance(session, sam, "savings", "S", 2023, 6, 20_000)
    add_balance(session, sam, "savings", "S", 2024, 12, 30_000)
    everyone = {r.year: r for r in run_forecast_baseline(session, None).history}
    assert everyone[2024].net_worth_minor == 100_000 + 30_000  # Alex carried forward, as on screen
    assert everyone[2024].source == "entries"


def test_defaults_use_earned_income_only(session: Session) -> None:
    uid = add_user(session, "Alex")
    add_w2(
        session,
        uid,
        2023,
        8_000_000,
        {"federal_income_tax_withheld": 1_000_000},
        other_income={"interest": 500_000, "long_term_capital_gain": 1_500_000},
    )
    add_w2(
        session,
        uid,
        2024,
        8_800_000,
        {"federal_income_tax_withheld": 1_100_000},
        other_income={"ordinary_dividends": 2_000_000},
    )
    result = run_forecast_baseline(session, uid)
    rows = {r.year: r for r in result.history}
    assert rows[2023].income_minor == 10_000_000  # gross, with investment income
    assert rows[2023].earned_income_minor == 8_000_000
    d = result.defaults
    assert (d.annual_income_minor, d.income_year) == (8_800_000, 2024)  # wages only
    assert d.income_growth_pct == 10.0  # earned: 8.0M -> 8.8M, not 10.0M -> 10.8M
    # The year's tax dollars (1,100,000) are carried by the earned income alone: 12.5%.
    assert d.effective_tax_rate_pct == pytest.approx(
        12.5, abs=0.02
    )  # tax is rebuilt from a 4-dp rate


def test_default_income_year_is_the_latest_year_with_earned_income(session: Session) -> None:
    uid = add_user(session, "Alex")
    add_w2(session, uid, 2022, 5_000_000, {"federal_income_tax_withheld": 500_000})
    # 2023 has investment income only: it is not an earned-income year.
    add_w2(session, uid, 2023, 0, {}, other_income={"interest": 700_000})
    d = run_forecast_baseline(session, uid).defaults
    assert (d.annual_income_minor, d.income_year) == (5_000_000, 2022)


@pytest.mark.parametrize(
    ("first", "last", "years", "expected"),
    [
        (100, 121, 2, 10.0),
        (100, 100, 5, 0.0),
        (200, 100, 1, -50.0),
        (0, 10, 1, None),
        (10, 0, 1, None),
        (1, 2, 0, None),
    ],
)
def test_cagr_pct(first: int, last: int, years: int, expected: float | None) -> None:
    assert cagr_pct(first, last, years) == expected
