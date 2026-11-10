"""Forecast history and defaults (`analytics/forecast_baseline.py`)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from personal_finance_analyzer.analytics.forecast_baseline import cagr_pct, run_forecast_baseline
from personal_finance_analyzer.db.models import NetWorthHistory
from tests.forecast_seed import SpendSeed, add_balance, add_user, add_w2

# Withholding on 80,000.00 of 2022 wages: federal 10,000 + Social Security 4,960 + Medicare 1,160.
# There is no Form 1040, so the year's tax is the headline total: the *estimated* federal tax
# (single, standard deduction $12,950: taxable $67,050, the tax table's $67,075 midpoint gives
# 1,027.50 + 3,780.00 + 22% x 25,300 = $10,374) plus the same FICA ($6,120) = $16,494.
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
        # 2023 wages 88,000.00: Social Security 6.2% = 5,456 and Medicare 1.45% = 1,276. The estimated
        # federal tax (single, standard deduction $13,850: taxable $74,150, table midpoint $74,175)
        # is 1,100 + 4,047 + 22% x 29,450 = $11,626, so the year's tax is 11,626 + 6,732 = $18,358.
        {
            "federal_income_tax_withheld": 1_100_000,
            "social_security_tax": 545_600,
            "medicare_tax": 127_600,
        },
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
    assert no_net_worth.tax_minor == 1_649_400 and no_net_worth.income_basis == "source_documents"


def test_spending_months_and_savings(session: Session) -> None:
    uid = _seed(session)
    rows = {r.year: r for r in run_forecast_baseline(session, uid).history}
    full, partial = rows[2023], rows[2024]
    assert (full.spending_minor, full.spending_months) == (1_180_000, 12)
    assert (partial.spending_minor, partial.spending_months) == (450_000, 3)
    # Tax is the summary's headline total tax, to the cent: 11,626 estimated federal + 6,732 FICA.
    assert full.tax_minor == 1_835_800
    assert full.savings_minor == 8_800_000 - 1_835_800 - 1_180_000  # 57,842.00
    assert partial.savings_minor is None  # a partial spending year has no savings figure
    assert rows[2022].savings_minor is None  # tax year without spending


def test_defaults_derive_from_latest_years(session: Session) -> None:
    uid = _seed(session)
    d = run_forecast_baseline(session, uid).defaults
    assert (d.net_worth_minor, d.net_worth_year) == (180_000, 2024)
    assert (d.annual_income_minor, d.income_year) == (8_800_000, 2023)
    # 1,835,800 of tax on 8,800,000 of earned income (all of it wages here).
    assert d.effective_tax_rate_pct == pytest.approx(20.86)
    # 2024 has only 3 months: annualised from them, and flagged.
    assert (d.annual_spending_minor, d.spending_year, d.spending_annualized) == (
        1_800_000,
        2024,
        True,
    )
    assert d.income_growth_pct == 10.0  # 8,000,000 -> 8,800,000 over one year
    assert d.spending_growth_pct is None  # only one complete spending year
    assert d.savings_rate_pct == pytest.approx(65.73, abs=0.01)  # 5,784,200 / 8,800,000


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
    # No return and no FICA on the forms, so the year's tax is the estimated federal tax on the whole
    # $108,000 of income: single, standard deduction $14,600, taxable $93,400 (table midpoint
    # $93,425) = 1,160 + 4,266 + 22% x 46,275 = 15,606.50, i.e. $15,607. It is all carried by the
    # earned income ($88,000): 15,607 / 88,000 = 17.74% (spread over the gross it would be 14.45%).
    assert rows[2024].tax_minor == 1_560_700
    assert d.effective_tax_rate_pct == pytest.approx(17.74)


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


def test_growth_is_clamped_to_the_api_bounds_and_flagged(session: Session) -> None:
    uid = add_user(session, "Alex")
    add_w2(session, uid, 2022, 1_000_000, {})  # a partial first year: $10k then $150k
    add_w2(session, uid, 2023, 15_000_000, {})
    session.flush()
    d = run_forecast_baseline(session, uid).defaults
    assert d.income_growth_pct == 50.0 and d.income_growth_clamped is True
    assert d.spending_growth_pct is None and d.spending_growth_clamped is False


def test_growth_within_bounds_is_not_flagged(session: Session) -> None:
    d = run_forecast_baseline(session, _seed(session)).defaults
    assert d.income_growth_pct == 10.0 and d.income_growth_clamped is False


def test_carried_balance_does_not_override_a_manual_year(session: Session) -> None:
    uid = add_user(session, "Alex")
    add_balance(session, uid, "checking", "Chk", 2022, 12, 100_000)
    add_balance(session, uid, "checking", "Chk", 2025, 6, 400_000)
    session.add(NetWorthHistory(user_id=uid, year=2023, amount_minor=777_000))
    session.flush()
    rows = {r.year: r for r in run_forecast_baseline(session, uid).history}
    assert (rows[2022].net_worth_minor, rows[2022].source) == (100_000, "entries")
    # 2023 has no entries of its own: the manual value wins over the carried 100,000.
    assert (rows[2023].net_worth_minor, rows[2023].source) == (777_000, "manual")
    assert rows[2023].as_of is None
    # 2024 has nothing at all: the carried balance fills in, labelled, from its real month.
    assert (rows[2024].net_worth_minor, rows[2024].source, rows[2024].as_of) == (
        100_000,
        "carried",
        "2022-12",
    )
    assert (rows[2025].net_worth_minor, rows[2025].source) == (400_000, "entries")


def test_two_people_are_decided_separately_per_year(session: Session) -> None:
    alex, sam = add_user(session, "Alex"), add_user(session, "Sam")
    add_balance(session, alex, "checking", "A", 2022, 12, 100_000)  # then stops
    add_balance(session, sam, "checking", "S", 2022, 12, 10_000)
    add_balance(session, sam, "checking", "S", 2025, 12, 50_000)
    session.add_all(
        [
            NetWorthHistory(user_id=alex, year=2023, amount_minor=300_000),
            NetWorthHistory(user_id=alex, year=2024, amount_minor=400_000),
        ]
    )
    session.flush()
    rows = {r.year: r for r in run_forecast_baseline(session, None).history}
    # Alex's manual value wins over his own carried balance; Sam is carried (nothing manual).
    assert (rows[2023].net_worth_minor, rows[2023].source) == (310_000, "mixed")
    assert (rows[2024].net_worth_minor, rows[2024].source) == (410_000, "mixed")
    assert rows[2023].manual_net_worth_minor == 300_000
    # 2025: Alex has neither entries nor manual: carried 100,000 + Sam's real 50,000.
    assert rows[2025].net_worth_minor == 150_000
