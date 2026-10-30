"""The per-user federal tax estimate: the pure `compute` and `run_tax_estimate` over stored forms.

Expected figures are hand-computed from the IRS rate tables / Tax Table (see the comments); they
are not produced by calling the code under test.
"""

from __future__ import annotations

import random
from typing import Any

import pytest
from sqlalchemy.orm import Session

from spend_analyzer.analytics.tax_estimate import (
    NOT_MODELED,
    EstimateConflict,
    EstimateInputs,
    EstimateResult,
    EstimateUnavailable,
    PersonInputs,
    compute,
    run_tax_estimate,
)
from spend_analyzer.core import tax_calc
from spend_analyzer.core.tax_params import FILING_STATUSES
from spend_analyzer.db.models import TaxProfile
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
        ),
    )
    fields: dict[str, Any] = {}
    for key, value in kw.items():
        plain = key in ("deduction_mode", "aged_or_blind_count")
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
    assert r.total_tax_minor == base.total_tax_minor - 2_000 * D - 1_000 * D + 300 * D
    assert r.total_payments_minor == 10_500 * D
    assert r.result_minor == r.total_payments_minor - r.total_tax_minor


def test_nonrefundable_credits_cannot_go_below_zero_but_refundable_can() -> None:
    r = _calc(wages=20_000, nonrefundable_credits=50_000, refundable_credits=1_200)
    assert r.income_tax_minor == 543 * D  # 5,400 taxable: Table midpoint 5,425 x 10% = 542.50
    assert r.total_tax_minor == -1_200 * D  # credits exceed the tax: floor at 0, then -refundable
    assert r.result_minor == 1_200 * D


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


def test_threshold_warnings() -> None:
    high = _calc(2024, wages=260_000, interest=10_000)
    assert any("Additional Medicare" in w for w in high.warnings)
    assert any("net investment income" in w for w in high.warnings)
    low = _calc(2024, wages=100_000, interest=10_000)
    assert not any("Additional Medicare" in w or "investment income tax" in w for w in low.warnings)
    # the joint threshold is higher
    assert not any("Additional Medicare" in w for w in _calc(2024, "mfj", wages=240_000).warnings)


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
