"""The per-user federal tax estimate: the pure `compute` and `run_tax_estimate` over stored forms.

Expected figures are hand-computed from the IRS rate tables / Tax Table (see the comments); they
are not produced by calling the code under test.
"""

from __future__ import annotations

import random
from typing import Any

import pytest
from sqlalchemy.orm import Session

from personal_finance_analyzer.analytics.tax_estimate import (
    NOT_MODELED,
    EstimateConflict,
    EstimateInputs,
    EstimateResult,
    EstimateUnavailable,
    PersonInputs,
    compute,
    run_tax_estimate,
)
from personal_finance_analyzer.core import tax_calc
from personal_finance_analyzer.core.tax_params import FILING_STATUSES, load_bundled
from personal_finance_analyzer.db.models import TaxProfile
from tests.analytics.tax_seed import add_form, add_profile, make_user

D = 100


def _calc(year: int = 2024, status: str = "single", **kw: Any) -> EstimateResult:
    """`compute` with whole-dollar keyword amounts (`wages`, `se`, `ss_wages`, other fields by name)."""
    persons = kw.pop("persons", None) or (
        PersonInputs(
            user_id=1,
            wages_minor=kw.pop("wages", 0) * D,
            self_employment_minor=kw.pop("se", 0) * D,
            social_security_wages_minor=kw.pop("ss_wages", 0) * D,
            retirement_deferral_minor=kw.pop("deferral", 0) * D,
            deferral_457b_minor=kw.pop("deferral_457b", 0) * D,
            medicare_tax_withheld_minor=kw.pop("medicare_withheld", 0) * D,
            social_security_tax_withheld_minor=kw.pop("ss_withheld", 0) * D,
            social_security_employers=kw.pop("employers", 0),
        ),
    )
    fields: dict[str, Any] = {}
    for key, value in kw.items():
        plain = key in ("deduction_mode", "aged_or_blind_count", "senior_count")
        fields[key if plain else f"{key}_minor"] = value if plain else value * D
    params = tax_calc.params_for(year)
    assert params is not None
    return compute(
        EstimateInputs(tax_year=year, filing_status=status, persons=persons, **fields), params
    )


# --- brackets and the worksheet ---------------------------------------------------------------------


def test_single_wages_only() -> None:
    # 80,000 - 14,600 standard = 65,400 taxable. Tax Table row 65,400-65,450 (midpoint 65,425):
    # 1,160 + 4,266 + 22% x (65,425 - 47,150) = 9,446.50 -> 9,447
    r = _calc(wages=80_000, federal_withheld=10_000)
    assert (r.gross_income_minor, r.agi_minor) == (80_000 * D, 80_000 * D)
    assert (r.deduction_minor, r.taxable_income_minor) == (14_600 * D, 65_400 * D)
    assert r.income_tax_minor == r.total_tax_minor == 9_447 * D
    assert r.result_minor == 553 * D and r.refund_minor == 553 * D and r.owed_minor == 0
    assert r.marginal_rate == 0.22
    assert r.effective_rate == round(9_447 / 80_000, 4)
    assert r.preferential_rate is None
    assert r.not_modeled == NOT_MODELED


def test_owed_when_withholding_is_short() -> None:
    r = _calc(wages=80_000, federal_withheld=5_000)
    assert r.result_minor == -4_447 * D and r.owed_minor == 4_447 * D and r.refund_minor == 0


def test_worksheet_with_qualified_dividends_and_long_term_gain() -> None:
    # gross 100,000 + 2,000 + 6,000 + 3,000 + 20,000 = 131,000; taxable 116,400; preferential =
    # 5,000 qualified + 20,000 long term. Ordinary part 91,400 (Table midpoint 91,425):
    # 1,160 + 4,266 + 22% x 44,275 = 15,166.50 -> 15,167. All 25,000 is above the 0% band
    # (47,025): 15% = 3,750. Total 18,917.
    r = _calc(
        wages=100_000,
        interest=2_000,
        ordinary_dividends=6_000,
        qualified_dividends=5_000,
        short_term_gain=3_000,
        long_term_gain=20_000,
        federal_withheld=15_000,
    )
    assert r.gross_income_minor == 131_000 * D
    assert r.taxable_income_minor == 116_400 * D
    assert r.preferential_income_minor == 25_000 * D
    assert (r.ordinary_tax_minor, r.preferential_tax_minor) == (15_167 * D, 3_750 * D)
    assert r.income_tax_minor == r.total_tax_minor == 18_917 * D
    assert r.preferential_rate == 0.15 and r.marginal_rate == 0.22


def test_qualified_dividends_are_capped_at_ordinary_dividends() -> None:
    r = _calc(ordinary_dividends=1_000, qualified_dividends=5_000, wages=50_000)
    assert r.qualified_dividends_minor == 1_000 * D
    assert any("Qualified dividends exceed" in w for w in r.warnings)


@pytest.mark.parametrize("status", FILING_STATUSES)
@pytest.mark.parametrize("year", [2022, 2023, 2024, 2025, 2026])
def test_every_year_and_status_computes_consistently(year: int, status: str) -> None:
    rng = random.Random(f"{year}{status}")
    for _ in range(25):
        r = _calc(
            year,
            status,
            wages=rng.randrange(0, 900_000),
            interest=rng.randrange(0, 20_000),
            ordinary_dividends=(div := rng.randrange(0, 50_000)),
            qualified_dividends=rng.randrange(0, div + 1),
            short_term_gain=rng.randrange(-20_000, 20_000),
            long_term_gain=rng.randrange(-30_000, 200_000),
            federal_withheld=rng.randrange(0, 100_000),
        )
        assert r.result_minor == r.total_payments_minor - r.total_tax_minor
        assert r.total_tax_minor >= 0
        assert r.income_tax_minor == r.ordinary_tax_minor + r.preferential_tax_minor
        assert r.taxable_income_minor >= 0
        assert r.income_tax_minor <= (
            tax_calc.regular_tax(year, status, r.taxable_income_minor) or 0
        )


def test_unknown_status_or_year_is_rejected() -> None:
    params = tax_calc.params_for(2024)
    assert params is not None
    inputs = EstimateInputs(
        tax_year=2024, filing_status="widow", persons=(PersonInputs(1, 0, 0, 0),)
    )
    with pytest.raises(ValueError):
        compute(inputs, params)
    with pytest.raises(ValueError):
        compute(
            EstimateInputs(tax_year=2025, filing_status="single", persons=()),
            params,
        )


# --- capital gains and losses ----------------------------------------------------------------------


def test_capital_loss_limit_single_and_carryover_reported() -> None:
    r = _calc(wages=50_000, short_term_gain=-5_000, long_term_gain=-1_000)
    assert r.capital_result_counted_minor == -3_000 * D
    assert r.capital_loss_carryover_out_minor == 3_000 * D
    assert r.gross_income_minor == 47_000 * D
    assert any("carries over to 2025" in w for w in r.warnings)


def test_capital_loss_limit_married_filing_separately_is_half() -> None:
    r = _calc(status="mfs", wages=50_000, short_term_gain=-5_000, long_term_gain=-1_000)
    assert r.capital_result_counted_minor == -1_500 * D
    assert r.capital_loss_carryover_out_minor == 4_500 * D


@pytest.mark.parametrize("status", ["mfj", "hoh", "qss"])
def test_capital_loss_limit_other_statuses(status: str) -> None:
    r = _calc(status=status, long_term_gain=-10_000)
    assert r.capital_result_counted_minor == -3_000 * D
    assert r.capital_loss_carryover_out_minor == 7_000 * D


def test_carryover_in_offsets_gains_first() -> None:
    r = _calc(wages=50_000, short_term_gain=1_000, capital_loss_carryover=2_000)
    assert r.capital_result_counted_minor == -1_000 * D  # under the limit: fully deducted
    assert r.capital_loss_carryover_out_minor == 0
    big = _calc(wages=50_000, long_term_gain=2_000, capital_loss_carryover=10_000)
    assert big.capital_result_counted_minor == -3_000 * D
    assert big.capital_loss_carryover_out_minor == 5_000 * D  # 8,000 loss - 3,000 limit
    assert big.preferential_income_minor == 0  # a net loss leaves no preferential income


def test_long_term_gain_is_reduced_by_a_short_term_loss() -> None:
    # net capital = -4,000 + 10,000 = 6,000; only 6,000 of the long-term gain is preferential
    r = _calc(wages=60_000, short_term_gain=-4_000, long_term_gain=10_000)
    assert r.capital_result_counted_minor == 6_000 * D
    assert r.preferential_income_minor == 6_000 * D


# --- deductions, credits, other taxes ---------------------------------------------------------------


def test_standard_deduction_with_aged_or_blind_boxes() -> None:
    # 2025 single: 15,750 + 2,000 per box; married (qss) boxes add 1,600
    assert _calc(2025, "single", wages=50_000, aged_or_blind_count=1).deduction_minor == 17_750 * D
    assert _calc(2025, "mfj", wages=50_000, aged_or_blind_count=2).deduction_minor == 34_700 * D
    assert _calc(2025, "hoh", wages=50_000).deduction_minor == 23_625 * D


def test_itemized_replaces_the_standard_deduction() -> None:
    r = _calc(wages=100_000, deduction_mode="itemized", itemized_deductions=30_000)
    assert r.deduction_minor == 30_000 * D and r.taxable_income_minor == 70_000 * D
    assert not any("below the standard" in w for w in r.warnings)
    low = _calc(wages=100_000, deduction_mode="itemized", itemized_deductions=5_000)
    assert low.deduction_minor == 5_000 * D
    assert any("below the standard deduction" in w for w in low.warnings)


def test_credits_taxes_and_payments() -> None:
    base = _calc(wages=80_000, federal_withheld=9_000)
    r = _calc(
        wages=80_000,
        federal_withheld=9_000,
        estimated_payments=1_000,
        other_payments=500,
        nonrefundable_credits=2_000,
        refundable_credits=1_000,
        other_taxes=300,
    )
    # nonrefundable credits and other taxes change the total tax; refundable credits are payments
    assert r.total_tax_minor == base.total_tax_minor - 2_000 * D + 300 * D
    assert r.total_payments_minor == (9_000 + 1_000 + 500 + 1_000) * D
    assert r.result_minor == r.total_payments_minor - r.total_tax_minor
    # the lines of the breakdown add up
    assert (
        r.income_tax_minor
        - r.nonrefundable_credits_minor
        + r.se_tax_minor
        + r.net_investment_income_tax_minor
        + r.additional_medicare_tax_minor
        + r.other_taxes_minor
        == r.total_tax_minor
    )
    assert (
        r.federal_withheld_minor
        + r.estimated_payments_minor
        + r.other_payments_minor
        + r.refundable_credits_minor
        + r.additional_medicare_withheld_minor
        + r.excess_social_security_credit_minor
        == r.total_payments_minor
    )


def test_nonrefundable_credits_are_capped_and_the_breakdown_shows_the_cap() -> None:
    r = _calc(wages=20_000, nonrefundable_credits=50_000, refundable_credits=1_200)
    assert r.income_tax_minor == 543 * D  # 5,400 taxable: Table midpoint 5,425 x 10% = 542.50
    assert r.nonrefundable_credits_minor == 543 * D  # only what the tax can absorb
    assert r.nonrefundable_credits_entered_minor == 50_000 * D
    assert r.total_tax_minor == 0  # never negative
    assert r.total_payments_minor == 1_200 * D and r.result_minor == 1_200 * D
    assert any("Nonrefundable credits" in w for w in r.warnings)


def test_refundable_credit_is_a_payment_not_negative_tax() -> None:
    # 2025 single, 30,000 wages, 3,000 earned income credit: taxable 14,250, Table midpoint 14,275:
    # 10% x 11,925 + 12% x 2,350 = 1,474.50 -> 1,475
    r = _calc(2025, wages=30_000, refundable_credits=3_000)
    assert r.income_tax_minor == r.total_tax_minor == 1_475 * D  # not 1,475 - 3,000
    assert r.total_payments_minor == 3_000 * D
    assert r.result_minor == 1_525 * D and r.refund_minor == 1_525 * D
    assert r.effective_rate == round(1_475 / 30_000, 4)  # not a negative rate


# --- self-employment tax -----------------------------------------------------------------------------


def test_se_tax_under_the_wage_base() -> None:
    # net earnings 100,000 x 92.35% = 92,350; wages 150,000 leave 18,600 under the 2024 base
    # (168,600): 12.4% x 18,600 = 2,306.40; Medicare 2.9% x 92,350 = 2,678.15 -> 4,984.55
    r = _calc(se=100_000, ss_wages=150_000)
    assert r.se_tax_minor == 498_455
    assert r.se_tax_deduction_minor == 249_228  # half, rounded half up
    assert r.agi_minor == r.gross_income_minor - r.se_tax_deduction_minor


def test_se_tax_wage_base_cap() -> None:
    # W-2 Social Security wages at or above the base: only the 2.9% Medicare part remains
    assert _calc(se=100_000, ss_wages=200_000).se_tax_minor == 267_815
    assert _calc(se=100_000, ss_wages=168_600).se_tax_minor == 267_815
    # the 12.4% never applies above the base even with no wages: 92,350 < 168,600 here, so full
    assert _calc(se=100_000).se_tax_minor == 1_145_140 + 267_815


def test_se_tax_none_below_400_and_per_person_cap() -> None:
    assert _calc(se=400).se_tax_minor == 0  # 369.40 net earnings
    assert _calc(se=435).se_tax_minor > 0  # 401.72
    # a joint return: each spouse has their own wage base, so two capped earners pay twice
    couple = (
        PersonInputs(1, 0, 100_000 * D, 168_600 * D),
        PersonInputs(2, 0, 100_000 * D, 0),
    )
    r = _calc(status="mfj", persons=couple)
    assert r.se_tax_minor == 267_815 + 1_145_140 + 267_815


# --- W-2 box 12 and warnings -------------------------------------------------------------------------


def test_box12_memo_does_not_change_the_numbers() -> None:
    plain = _calc(wages=80_000, federal_withheld=10_000)
    with_memo = _calc(wages=80_000, federal_withheld=10_000, deferral=10_000, hsa_employer=1_000)
    assert with_memo.wages_minor == plain.wages_minor
    assert with_memo.total_tax_minor == plain.total_tax_minor
    assert with_memo.agi_minor == plain.agi_minor
    assert (with_memo.retirement_deferral_minor, with_memo.hsa_employer_minor) == (
        10_000 * D,
        1_000 * D,
    )


def test_deferral_over_the_limit_warns_per_person() -> None:
    over = _calc(2024, wages=100_000, deferral=25_000)  # limit 23,000
    assert any("over the 2024 limit of $23,000" in w for w in over.warnings)
    two = (
        PersonInputs(1, 100_000 * D, 0, 0, retirement_deferral_minor=20_000 * D),
        PersonInputs(2, 100_000 * D, 0, 0, retirement_deferral_minor=20_000 * D),
    )
    assert not any("deferral" in w for w in _calc(status="mfj", persons=two).warnings)
    one_over = (
        PersonInputs(1, 100_000 * D, 0, 0, retirement_deferral_minor=20_000 * D),
        PersonInputs(2, 100_000 * D, 0, 0, retirement_deferral_minor=30_000 * D),
    )
    warned = [w for w in _calc(status="mfj", persons=one_over).warnings if "deferral" in w]
    assert len(warned) == 1 and "user 2" in warned[0]


def test_457b_has_its_own_limit_and_the_catch_up_is_mentioned() -> None:
    # 20,000 of 401(k) plus 20,000 of 457(b): neither is over 23,000 on its own
    r = _calc(2024, wages=100_000, deferral=40_000, deferral_457b=20_000)
    assert not any("deferral" in w for w in r.warnings)
    over_457 = _calc(2024, wages=100_000, deferral=30_000, deferral_457b=30_000)
    assert any("457(b)" in w for w in over_457.warnings)
    assert not any("Pre-tax 401(k)" in w for w in over_457.warnings)
    over_401 = _calc(2024, wages=100_000, deferral=25_000)
    warning = next(w for w in over_401.warnings if "Pre-tax 401(k)" in w)
    assert "50 or older" in warning and "AA, BB" in warning  # catch-up and Roth caveats


# --- net investment income tax, Additional Medicare Tax, excess Social Security -------------------------


def test_niit_single_300k_wages_and_50k_investment_income() -> None:
    # AGI 350,000; 3.8% x min(50,000, 350,000 - 200,000) = 1,900
    r = _calc(2024, wages=300_000, interest=50_000)
    assert r.net_investment_income_tax_minor == 1_900 * D
    assert r.total_tax_minor == (
        r.income_tax_minor + r.net_investment_income_tax_minor + r.additional_medicare_tax_minor
    )
    assert any("Net investment income tax of $1,900" in w for w in r.warnings)


def test_niit_is_limited_by_the_excess_over_the_threshold_and_needs_investment_income() -> None:
    # AGI 210,000: only 10,000 is above the threshold, though 50,000 is investment income
    assert _calc(2024, wages=160_000, interest=50_000).net_investment_income_tax_minor == 380 * D
    assert _calc(2024, wages=500_000).net_investment_income_tax_minor == 0
    assert _calc(2024, wages=100_000, interest=10_000).net_investment_income_tax_minor == 0
    # a joint return has the higher 250,000 threshold; a net capital loss lowers the base
    assert _calc(2024, "mfj", wages=240_000, interest=5_000).net_investment_income_tax_minor == 0
    loss = _calc(2024, wages=300_000, interest=1_000, long_term_gain=-10_000)
    assert loss.net_investment_income_tax_minor == 0  # 1,000 - 3,000 limited loss -> below zero


def test_additional_medicare_tax_qss_threshold_is_200k_not_the_joint_250k() -> None:
    # IRC 3101(b)(2) / Form 8959: 250,000 only for a joint return; a qualifying surviving spouse
    # (like single and head of household) starts at 200,000: 0.9% x 40,000 = 360 owed
    r = _calc(2025, "qss", wages=240_000, ss_wages=240_000)
    assert r.additional_medicare_tax_minor == 360 * D
    assert _calc(2025, "mfj", wages=240_000, ss_wages=240_000).additional_medicare_tax_minor == 0
    # the NIIT threshold, in contrast, is 250,000 for a surviving spouse (IRC 1411)
    assert load_bundled().years[2025].niit_threshold_minor["qss"] == 250_000 * D


def test_additional_medicare_tax_mfj_two_180k_earners() -> None:
    # neither employer withholds the extra 0.9% (each is under 200,000), but together they are
    # 110,000 over the 250,000 joint threshold: 0.9% x 110,000 = 990 owed
    couple = tuple(
        PersonInputs(i, 180_000 * D, 0, 180_000 * D, medicare_tax_withheld_minor=261_000)
        for i in (1, 2)
    )
    r = _calc(2024, "mfj", persons=couple)
    assert r.additional_medicare_tax_minor == 990 * D
    assert r.additional_medicare_withheld_minor == 0
    assert r.total_tax_minor == r.income_tax_minor + 990 * D
    assert r.total_payments_minor == 0 and r.result_minor == -r.total_tax_minor


def test_additional_medicare_withheld_is_a_payment_not_a_discount() -> None:
    # single, 260,000 wages: owed 0.9% x 60,000 = 540; the employer withheld the same 540 on top of
    # 1.45% x 260,000 = 3,770 (box 6 = 4,310)
    with_box6 = _calc(2024, wages=260_000, federal_withheld=50_000, medicare_withheld=4_310)
    assert with_box6.additional_medicare_tax_minor == 540 * D
    assert with_box6.additional_medicare_withheld_minor == 540 * D
    no_box6 = _calc(2024, wages=260_000, federal_withheld=50_000)
    assert no_box6.additional_medicare_withheld_minor == 0
    # the tax is the same; only the withheld part moves into payments
    assert with_box6.total_tax_minor == no_box6.total_tax_minor
    assert with_box6.result_minor == no_box6.result_minor + 540 * D
    # a plain 1.45% box 6 contains none of it (and rounding noise under $1 is ignored)
    plain = _calc(2024, wages=100_000, medicare_withheld=1_450)
    assert plain.additional_medicare_withheld_minor == 0


def test_additional_medicare_tax_on_self_employment_uses_what_the_wages_left_unused() -> None:
    # wages 150,000 leave 50,000 of the 200,000 single threshold; SE net earnings
    # 92.35% x 100,000 = 92,350 -> 42,350 above it: 0.9% x 42,350 = 381.15
    r = _calc(2024, wages=150_000, se=100_000)
    assert r.additional_medicare_tax_minor == 38_115
    assert _calc(2024, se=100_000).additional_medicare_tax_minor == 0  # under the threshold


def test_additional_medicare_tax_threshold_for_married_filing_separately() -> None:
    r = _calc(2024, "mfs", wages=150_000)  # threshold 125,000: 0.9% x 25,000 = 225
    assert r.additional_medicare_tax_minor == 225 * D


def test_excess_social_security_two_employers_2025() -> None:
    # two employers paying 120,000 each: 2 x 7,440 = 14,880 withheld; the 2025 maximum is
    # 6.2% x 176,100 = 10,918.20, so 3,961.80 comes back as a credit (Schedule 3 line 11)
    r = _calc(
        2025,
        wages=240_000,
        federal_withheld=40_000,
        ss_withheld=14_880,
        ss_wages=240_000,
        employers=2,
    )
    assert r.excess_social_security_credit_minor == 396_180
    assert r.total_payments_minor == 40_000 * D + 396_180
    assert any("above the annual maximum" in w for w in r.warnings)


def test_no_excess_social_security_credit_for_one_employer_or_split_spouses() -> None:
    # a single employer that over-withheld must refund it itself
    one = _calc(2025, wages=200_000, ss_withheld=12_400, ss_wages=200_000, employers=1)
    assert one.excess_social_security_credit_minor == 0
    # spouses are each their own person: neither exceeds the maximum on their own
    spouses = tuple(
        PersonInputs(
            i,
            120_000 * D,
            0,
            120_000 * D,
            social_security_tax_withheld_minor=744_000,
            social_security_employers=1,
        )
        for i in (1, 2)
    )
    assert _calc(2025, "mfj", persons=spouses).excess_social_security_credit_minor == 0
    # at or below the maximum with two employers: nothing
    exact = _calc(2025, wages=170_000, ss_withheld=10_540, ss_wages=170_000, employers=2)
    assert exact.excess_social_security_credit_minor == 0


# --- senior deduction, age 65 / blind boxes ------------------------------------------------------------


def test_senior_deduction_2025_for_one_person() -> None:
    # single, 70 years old (one age box), 60,000 wages: standard 15,750 + 2,000, senior 6,000
    r = _calc(2025, wages=60_000, aged_or_blind_count=1, senior_count=1)
    assert r.deduction_minor == 17_750 * D and r.senior_deduction_minor == 6_000 * D
    assert r.taxable_income_minor == 36_250 * D
    twin = _calc(2025, wages=60_000, adjustments=6_000, aged_or_blind_count=1)
    assert r.income_tax_minor == twin.income_tax_minor  # same taxable income


def test_senior_deduction_phases_out_at_six_percent_over_75k() -> None:
    # MAGI 95,000: 6% x 20,000 = 1,200 -> 4,800
    r = _calc(2025, wages=95_000, senior_count=1)
    assert r.senior_deduction_minor == 4_800 * D
    # MFJ, both 65+, MAGI 170,000: each person's 6,000 - 6% x 20,000 = 4,800 (IRC 151(d)(5)(C)(ii))
    j = _calc(2025, "mfj", wages=170_000, senior_count=2)
    assert j.senior_deduction_minor == 9_600 * D
    # MAGI 200,000: each 6,000 - 3,000, so 6,000 in all; fully gone at 250,000
    assert _calc(2025, "mfj", wages=200_000, senior_count=2).senior_deduction_minor == 6_000 * D
    assert _calc(2025, "mfj", wages=250_000, senior_count=2).senior_deduction_minor == 0
    assert (
        _calc(2025, wages=175_000, senior_count=1).senior_deduction_minor == 0
    )  # fully phased out


def test_senior_deduction_applies_to_itemizers_too_and_only_2025_to_2028() -> None:
    item = _calc(
        2025, wages=60_000, deduction_mode="itemized", itemized_deductions=20_000, senior_count=1
    )
    assert item.deduction_minor == 20_000 * D and item.senior_deduction_minor == 6_000 * D
    assert _calc(2024, wages=60_000, senior_count=1).senior_deduction_minor == 0  # not yet law
    assert _calc(2026, wages=60_000, senior_count=1).senior_deduction_minor == 6_000 * D


def test_senior_deduction_is_not_available_when_married_filing_separately() -> None:
    r = _calc(2025, "mfs", wages=60_000, senior_count=1)
    assert r.senior_deduction_minor == 0
    assert any("married persons filing separately" in w for w in r.warnings)


def test_aged_blind_boxes_are_capped_by_filing_status() -> None:
    single = _calc(2025, wages=60_000, aged_or_blind_count=4)
    assert single.deduction_minor == (15_750 + 2 * 2_000) * D
    assert any("at most 2" in w for w in single.warnings)
    joint = _calc(2025, "mfj", wages=60_000, aged_or_blind_count=4)
    assert joint.deduction_minor == (31_500 + 4 * 1_600) * D
    assert not any("at most" in w for w in joint.warnings)


def test_tips_overtime_and_car_loan_interest_are_listed_as_not_modeled() -> None:
    text = " ".join(NOT_MODELED)
    assert "tips" in text and "overtime" in text and "car loan" in text and "Schedule 1-A" in text


# --- from stored forms ---------------------------------------------------------------------------------


def _w2(
    session: Session, user: int, year: int, wages: int, *, deferral: int = 0, **extra: int
) -> None:
    income = {"salary_wages": wages * D}
    if deferral:
        income["retirement_deferral"] = deferral * D
    add_form(
        session,
        user,
        year,
        "w2",
        income=income,
        taxes={
            "federal_income_tax_withheld": extra.pop("withheld", 0) * D,
            **{k: v * D for k, v in extra.items()},
        },
        tin="0001",
    )


def test_box12_deferral_from_a_stored_w2_is_never_subtracted(session: Session) -> None:
    u = make_user(session, "a")
    _w2(session, u, 2024, 80_000, deferral=30_000, withheld=10_000)
    est = run_tax_estimate(session, u, 2024)
    assert est.calc.wages_minor == 80_000 * D
    assert est.calc.retirement_deferral_minor == 30_000 * D
    assert est.calc.total_tax_minor == 9_447 * D
    assert any("over the 2024 limit" in w for w in est.warnings)


def test_ss_wages_come_from_the_box4_tax_withheld_when_present(session: Session) -> None:
    u = make_user(session, "a")
    # box 4 of 10,453.20 implies 168,600 of Social Security wages (the 2024 wage base)
    add_form(
        session,
        u,
        2024,
        "w2",
        income={"salary_wages": 150_000 * D},
        taxes={"social_security_tax": 1_045_320},
        tin="0001",
    )
    add_form(session, u, 2024, "1099-nec", income={"self_employment": 100_000 * D}, tin="0002")
    est = run_tax_estimate(session, u, 2024)
    assert est.calc.se_tax_minor == 267_815  # no 12.4% room left under the wage base


def test_defaults_without_a_profile(session: Session) -> None:
    u = make_user(session, "a")
    _w2(session, u, 2024, 80_000, withheld=10_000)
    est = run_tax_estimate(session, u, 2024)
    assert (est.calc.filing_status, est.filing_status_source) == ("single", "default")
    assert est.calc.deduction_mode == "standard"
    assert est.scope == "individual" and est.household_user_ids == (u,)
    assert est.share is None
    assert est.user_total_tax_minor == est.calc.total_tax_minor == 9_447 * D
    assert est.user_result_minor == 553 * D
    assert est.cross_check is None
    assert any("No tax profile" in w for w in est.warnings)
    assert est.parameters_source == "bundled" and est.parameter_sources


def test_filing_status_defaults_from_the_return_but_the_return_is_not_an_input(
    session: Session,
) -> None:
    u = make_user(session, "a")
    _w2(session, u, 2024, 90_000, withheld=12_000)
    before = run_tax_estimate(session, u, 2024)
    add_form(
        session,
        u,
        2024,
        "1040",
        income={"salary_wages": 1 * D},  # absurd figures: must change nothing
        taxes={"total_federal_tax": 999_999 * D, "federal_income_tax_withheld": 5 * D},
        filing_status="hoh",
    )
    after = run_tax_estimate(session, u, 2024)
    assert (after.calc.filing_status, after.filing_status_source) == ("hoh", "return")
    # same inputs as an explicit hoh profile would give
    expected = _calc(2024, "hoh", wages=90_000, federal_withheld=12_000)
    assert after.calc.total_tax_minor == expected.total_tax_minor
    assert after.calc.wages_minor == before.calc.wages_minor == 90_000 * D


def test_cross_check_deltas(session: Session) -> None:
    u = make_user(session, "a")
    _w2(session, u, 2024, 80_000, withheld=10_000)
    add_form(
        session,
        u,
        2024,
        "1040",
        taxes={
            "total_federal_tax": 9_000 * D,
            "federal_income_tax_withheld": 9_900 * D,
            "refund": 900 * D,
        },
        filing_status="single",
    )
    est = run_tax_estimate(session, u, 2024)
    cc = est.cross_check
    assert cc is not None and not cc.household
    assert cc.return_total_tax_minor == 9_000 * D
    assert cc.delta_total_tax_minor == (9_447 - 9_000) * D  # estimate minus return
    assert cc.delta_withheld_minor == (10_000 - 9_900) * D
    assert cc.return_result_minor == 900 * D
    assert cc.delta_result_minor == (553 - 900) * D


def test_profile_inputs_are_used(session: Session) -> None:
    u = make_user(session, "a")
    _w2(session, u, 2024, 100_000, withheld=14_000)
    add_profile(
        session,
        u,
        2024,
        "hoh",
        deduction_mode="itemized",
        itemized_deductions_minor=30_000 * D,
        adjustments_minor=2_000 * D,
        other_income_minor=1_000 * D,
        nonrefundable_credits_minor=500 * D,
        estimated_payments_minor=1_000 * D,
        capital_loss_carryover_minor=1_500 * D,
    )
    est = run_tax_estimate(session, u, 2024)
    c = est.calc
    assert (c.filing_status, est.filing_status_source) == ("hoh", "profile")
    assert c.gross_income_minor == (100_000 + 1_000 - 1_500) * D
    assert c.agi_minor == c.gross_income_minor - 2_000 * D
    assert c.deduction_minor == 30_000 * D
    assert c.nonrefundable_credits_minor == 500 * D
    assert c.total_payments_minor == 15_000 * D


def test_unsupported_year_raises(session: Session) -> None:
    u = make_user(session, "a")
    with pytest.raises(EstimateUnavailable, match="2020"):
        run_tax_estimate(session, u, 2020)
    with pytest.raises(EstimateUnavailable):
        run_tax_estimate(session, u, 2030)


def test_no_data_still_answers_with_a_warning(session: Session) -> None:
    u = make_user(session, "a")
    est = run_tax_estimate(session, u, 2024)
    assert est.calc.total_tax_minor == 0 and est.calc.gross_income_minor == 0
    assert est.calc.effective_rate is None
    assert any("No W-2/1099" in w for w in est.warnings)


# --- married filing jointly ---------------------------------------------------------------------------


def _couple(session: Session) -> tuple[int, int]:
    a, b = make_user(session, "a"), make_user(session, "b")
    _w2(session, a, 2024, 120_000, withheld=15_000)
    _w2(session, b, 2024, 60_000, withheld=5_000)
    return a, b


def test_household_is_computed_once_and_allocated_by_gross_share(session: Session) -> None:
    a, b = _couple(session)
    add_profile(session, a, 2024, "mfj", joint_with_user_id=b, estimated_payments_minor=2_000 * D)
    # B has no profile: the link is found from A's side
    ea, eb = run_tax_estimate(session, a, 2024), run_tax_estimate(session, b, 2024)
    for est in (ea, eb):
        assert est.scope == "household_allocation"
        assert est.household_user_ids == (a, b)
        assert est.calc.filing_status == "mfj"
        assert est.calc.gross_income_minor == 180_000 * D
        assert est.calc.wages_minor == 180_000 * D
    # 180,000 - 29,200 = 150,800 taxable; one MFJ computation (Table 2024):
    expected = _calc(2024, "mfj", wages=180_000, federal_withheld=20_000, estimated_payments=2_000)
    assert ea.calc.total_tax_minor == eb.calc.total_tax_minor == expected.total_tax_minor
    assert ea.share == pytest.approx(2 / 3) and eb.share == pytest.approx(1 / 3)
    assert ea.user_total_tax_minor + eb.user_total_tax_minor == expected.total_tax_minor
    # payments are each user's own: A withheld 15,000 + 2,000 estimated; B withheld 5,000
    assert ea.user_payments_minor == 17_000 * D and eb.user_payments_minor == 5_000 * D
    assert ea.user_payments_minor + eb.user_payments_minor == ea.calc.total_payments_minor
    assert (ea.user_result_minor + eb.user_result_minor) == expected.result_minor
    assert any("allocated" in w for w in ea.warnings)


def test_allocation_sums_exactly_with_awkward_shares(session: Session) -> None:
    a, b = make_user(session, "a"), make_user(session, "b")
    _w2(session, a, 2024, 33_333, withheld=1_000)
    _w2(session, b, 2024, 66_667, withheld=1_000)
    add_profile(session, a, 2024, "mfj", joint_with_user_id=b)
    add_profile(session, b, 2024, "mfj", joint_with_user_id=a)
    ea, eb = run_tax_estimate(session, a, 2024), run_tax_estimate(session, b, 2024)
    assert ea.user_total_tax_minor + eb.user_total_tax_minor == ea.calc.total_tax_minor


def test_household_with_zero_income_splits_equally(session: Session) -> None:
    a, b = make_user(session, "a"), make_user(session, "b")
    add_profile(session, a, 2024, "mfj", joint_with_user_id=b, other_taxes_minor=1_001)
    ea, eb = run_tax_estimate(session, a, 2024), run_tax_estimate(session, b, 2024)
    assert ea.share == eb.share == 0.5
    assert ea.user_total_tax_minor + eb.user_total_tax_minor == 1_001


def test_both_profiles_inputs_are_summed(session: Session) -> None:
    a, b = _couple(session)
    add_profile(
        session,
        a,
        2024,
        "mfj",
        joint_with_user_id=b,
        adjustments_minor=1_000 * D,
        age65_or_blind_count=1,
    )
    add_profile(
        session,
        b,
        2024,
        "mfj",
        joint_with_user_id=a,
        adjustments_minor=500 * D,
        age65_or_blind_count=1,
    )
    est = run_tax_estimate(session, a, 2024)
    assert est.calc.adjustments_minor == 1_500 * D
    assert est.calc.deduction_minor == (29_200 + 2 * 1_550) * D


def test_joint_profile_without_a_spouse_profile_carries_the_household_boxes(
    session: Session,
) -> None:
    """The spouse is not an app user (or has no profile): one mfj profile holds both spouses'
    boxes, so 4 boxes and 2 seniors all count."""
    u = make_user(session, "a")
    _w2(session, u, 2025, 60_000, withheld=5_000)
    add_profile(session, u, 2025, "mfj", age65_or_blind_count=4, age65_count=2)  # no linked spouse
    est = run_tax_estimate(session, u, 2025)
    assert est.calc.deduction_minor == (31_500 + 4 * 1_600) * D
    assert est.calc.senior_deduction_minor == 12_000 * D
    assert not any("counts more age 65" in w for w in est.warnings)


def test_linked_spouse_without_a_profile_still_gets_the_household_total(session: Session) -> None:
    a, b = _couple(session)
    add_profile(
        session, a, 2024, "mfj", joint_with_user_id=b, age65_or_blind_count=3, age65_count=2
    )
    est = run_tax_estimate(session, a, 2024)
    assert est.calc.deduction_minor == (29_200 + 3 * 1_550) * D
    assert not any("counts more age 65" in w for w in est.warnings)


def test_two_linked_profiles_are_clamped_per_person_then_summed(session: Session) -> None:
    a, b = _couple(session)
    # A (wrongly) enters the household total; B enters their own: A is cut to 2 boxes / 1 senior
    add_profile(
        session, a, 2025, "mfj", joint_with_user_id=b, age65_or_blind_count=4, age65_count=2
    )
    add_profile(session, b, 2025, "mfj", joint_with_user_id=a, age65_or_blind_count=1)
    _w2(session, a, 2025, 120_000, withheld=15_000)
    _w2(session, b, 2025, 60_000, withheld=5_000)
    est = run_tax_estimate(session, a, 2025)
    assert est.calc.deduction_minor == (31_500 + 3 * 1_600) * D  # 2 + 1 boxes
    assert any("counts more age 65" in w and "own boxes" in w for w in est.warnings)
    # both spouses each at their own maximum: 2 + 2 = 4 boxes, 1 + 1 seniors, no note
    for uid, other in ((a, b), (b, a)):
        row = session.query(TaxProfile).filter_by(user_id=uid, tax_year=2025).one()
        row.age65_or_blind_count, row.age65_count = 2, 1
        row.joint_with_user_id = other
    session.commit()
    full = run_tax_estimate(session, b, 2025)
    assert full.calc.deduction_minor == (31_500 + 4 * 1_600) * D
    assert not any("counts more age 65" in w for w in full.warnings)
    # MAGI 180,000: each person's 6,000 - 6% x 30,000 = 4,200
    assert full.calc.senior_deduction_minor == 8_400 * D


def test_non_joint_profile_rows_with_household_counts_are_clamped(session: Session) -> None:
    u = make_user(session, "a")
    _w2(session, u, 2025, 60_000, withheld=5_000)
    add_profile(session, u, 2025, "single", age65_or_blind_count=4, age65_count=2)  # old row
    est = run_tax_estimate(session, u, 2025)
    assert est.calc.deduction_minor == (15_750 + 2 * 2_000) * D
    assert est.calc.senior_deduction_minor == 6_000 * D
    assert any("counts more age 65" in w for w in est.warnings)


def test_conflicting_profiles_raise(session: Session) -> None:
    a, b = _couple(session)
    c = make_user(session, "c")
    add_profile(session, a, 2024, "mfj", joint_with_user_id=b)
    add_profile(session, b, 2024, "single")
    with pytest.raises(EstimateConflict):
        run_tax_estimate(session, a, 2024)
    with pytest.raises(EstimateConflict):
        run_tax_estimate(session, b, 2024)
    session.query(TaxProfile).filter_by(user_id=b).delete()
    add_profile(session, b, 2024, "mfj", joint_with_user_id=c)  # names someone else
    with pytest.raises(EstimateConflict):
        run_tax_estimate(session, a, 2024)


def test_joint_cross_check_uses_the_household_and_either_spouses_return(session: Session) -> None:
    a, b = _couple(session)
    add_profile(session, a, 2024, "mfj", joint_with_user_id=b)
    add_form(
        session,
        b,
        2024,
        "1040",
        taxes={"total_federal_tax": 20_000 * D, "refund": 100 * D},
        filing_status="mfj",
    )
    est = run_tax_estimate(session, a, 2024)
    assert est.cross_check is not None and est.cross_check.household
    assert est.cross_check.return_total_tax_minor == 20_000 * D
    assert est.cross_check.delta_total_tax_minor == est.calc.total_tax_minor - 20_000 * D
    assert not any("not like for like" in w for w in est.warnings)


def test_return_status_mismatch_is_warned(session: Session) -> None:
    u = make_user(session, "a")
    _w2(session, u, 2024, 80_000, withheld=10_000)
    add_profile(session, u, 2024, "single")
    add_form(session, u, 2024, "1040", taxes={"total_federal_tax": 1}, filing_status="mfj")
    est = run_tax_estimate(session, u, 2024)
    assert any("not like for like" in w for w in est.warnings)
