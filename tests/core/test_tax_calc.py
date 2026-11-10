"""Tests for `core.tax_calc` (plan 2.6): worksheet-based ordinary/preferential split, and the
bracket data in `core/tax_data/federal_params.yaml` (schema tests are in `test_tax_params.py`)."""

from __future__ import annotations

import random
from itertools import pairwise
from typing import Any

import pytest

from spend_analyzer.core import tax_calc
from spend_analyzer.core.tax_calc import (
    bracket_tax,
    estimate_tax_split,
    has_table,
    regular_tax,
    supported_years,
    worksheet_tax,
)
from spend_analyzer.core.tax_params import Table, load_bundled

D = 100  # cents per dollar
STATUSES = ("single", "mfj", "mfs", "hoh", "qss")
RATES_BP = (1000, 1200, 2200, 2400, 3200, 3500, 3700)


def _raw() -> dict[int, dict[str, Table]]:
    return {year: dict(p.tables) for year, p in load_bundled().years.items()}


# --------------------------------------------------------------------------------------------
# Data-file validation
# --------------------------------------------------------------------------------------------


def test_ships_expected_years_and_statuses() -> None:
    assert supported_years() == (2022, 2023, 2024, 2025, 2026)
    for year in supported_years():
        for status in STATUSES:
            assert has_table(year, status)


def test_data_file_structure() -> None:
    raw = _raw()
    for year, statuses in raw.items():
        assert set(statuses) == set(STATUSES), year
        for status, table in statuses.items():
            thresholds = [t for t, _ in table.brackets]
            rates = [r for _, r in table.brackets]
            assert thresholds[0] == 0
            assert thresholds == sorted(set(thresholds)), (year, status)
            assert tuple(rates) == RATES_BP
            assert all(t % D == 0 for t in thresholds)  # whole dollars, stored as cents
            assert 0 < table.ltcg_0_to < table.ltcg_15_to
            # the 0% LTCG band ends within the 12% bracket (or the 10% one) for every status
            assert thresholds[1] < table.ltcg_0_to < thresholds[3]


def test_qss_uses_mfj_tables() -> None:
    raw = _raw()
    for year in raw:
        assert raw[year]["qss"] == raw[year]["mfj"]


def test_thresholds_grow_year_over_year() -> None:
    raw = _raw()
    years = sorted(raw)
    for status in ("single", "mfj", "hoh"):
        for a, b in pairwise(years):
            assert raw[b][status].ltcg_0_to > raw[a][status].ltcg_0_to
            assert raw[b][status].ltcg_15_to > raw[a][status].ltcg_15_to
            for (ta, _), (tb, _) in zip(
                raw[a][status].brackets, raw[b][status].brackets, strict=True
            ):
                assert tb >= ta


@pytest.mark.parametrize(
    ("year", "status", "ltcg_0", "ltcg_15", "top_bracket"),
    [  # spot checks against the IRS revenue procedures
        (2022, "single", 41_675, 459_750, 539_900),
        (2022, "hoh", 55_800, 488_500, 539_900),
        (2023, "mfj", 89_250, 553_850, 693_750),
        (2024, "single", 47_025, 518_900, 609_350),
        (2024, "mfs", 47_025, 291_850, 365_600),
        (2025, "mfj", 96_700, 600_050, 751_600),
        (2025, "hoh", 64_750, 566_700, 626_350),
        (2026, "single", 49_450, 545_500, 640_600),  # Rev. Proc. 2025-32 sections 4.01, 4.03
        (2026, "mfj", 98_900, 613_700, 768_700),
        (2026, "mfs", 49_450, 306_850, 384_350),
        (2026, "hoh", 66_200, 579_600, 640_600),
    ],
)
def test_spot_check_published_figures(
    year: int, status: str, ltcg_0: int, ltcg_15: int, top_bracket: int
) -> None:
    table = _raw()[year][status]
    assert table.ltcg_0_to == ltcg_0 * D
    assert table.ltcg_15_to == ltcg_15 * D
    assert table.brackets[-1][0] == top_bracket * D


# --------------------------------------------------------------------------------------------
# Hand-computed worked examples (IRS Qualified Dividends and Capital Gain Tax Worksheet)
# --------------------------------------------------------------------------------------------


def test_ordinary_tax_matches_tax_table_and_computation() -> None:
    # 2024 single, $50,000: Tax Table row 50,000-50,050, midpoint 50,025:
    #   1,160 + 4,266 + 22% * (50,025 - 47,150) = 6,058.50 -> 6,059
    assert regular_tax(2024, "single", 50_000 * D) == 6_059 * D
    # at $100,000 the Tax Computation Worksheet is used: 1,160 + 4,266 + 22% * 52,850 = 17,053
    assert regular_tax(2024, "single", 100_000 * D) == 17_053 * D
    assert bracket_tax(2024, "single", 100_000 * D) == 17_053 * D
    assert regular_tax(2024, "single", 0) == 0
    assert regular_tax(2024, "single", 4 * D) == 0
    assert regular_tax(2024, "single", 10 * D) == 1 * D  # row 5-15: midpoint 10 * 10%


def test_example_single_2024() -> None:
    # taxable 100,000; qualified dividends 5,000; net LTCG 15,000 => preferential 20,000.
    # line 7 = 80,000 (table midpoint 80,025: 1,160 + 4,266 + 22% * 32,875 = 12,658.50 -> 12,659)
    # 0% band (47,025) is already filled by ordinary income; 15% on all 20,000 = 3,000
    # line 23 = 15,659 < line 24 (17,053)
    split = estimate_tax_split(
        tax_year=2024,
        filing_status="single",
        taxable_income_minor=100_000 * D,
        qualified_dividends_minor=5_000 * D,
        net_ltcg_minor=15_000 * D,
        income_tax_minor=15_659 * D,
    )
    assert split is not None
    assert split.ordinary_minor == 12_659 * D
    assert split.preferential_minor == 3_000 * D
    assert split.basis == "worksheet"
    assert split.approximate is False


def test_example_mfj_2024_zero_percent_band_used() -> None:
    # taxable 120,000, net LTCG 30,000; ordinary 90,000 (table midpoint 90,025:
    # 2,320 + 12% * 66,825 = 10,339). Part of the gain (94,050 - 90,000 = 4,050) is taxed at 0%,
    # the remaining 25,950 at 15% = 3,892.50 -> 3,893. Total 14,232.
    assert (
        worksheet_tax(
            tax_year=2024,
            filing_status="mfj",
            taxable_income_minor=120_000 * D,
            qualified_dividends_minor=0,
            net_ltcg_minor=30_000 * D,
        )
        == 14_232 * D
    )
    for status in ("mfj", "qss"):
        split = estimate_tax_split(
            tax_year=2024,
            filing_status=status,
            taxable_income_minor=120_000 * D,
            qualified_dividends_minor=0,
            net_ltcg_minor=30_000 * D,
            income_tax_minor=14_232 * D,
        )
        assert split is not None
        assert (split.ordinary_minor, split.preferential_minor) == (10_339 * D, 3_893 * D)
        assert split.approximate is False


def test_example_mfs_2023() -> None:
    # taxable 60,000; qualified 2,000 + LTCG 8,000 => ordinary 50,000
    # (midpoint 50,025: 1,100 + 12% * 33,725 + 22% * 5,300 = 6,313); gains all above the 0% band
    # (44,625) => 15% * 10,000 = 1,500. Total 7,813 (regular tax on 60,000 would be 8,513).
    split = estimate_tax_split(
        tax_year=2023,
        filing_status="mfs",
        taxable_income_minor=60_000 * D,
        qualified_dividends_minor=2_000 * D,
        net_ltcg_minor=8_000 * D,
        income_tax_minor=7_813 * D,
    )
    assert split is not None
    assert (split.ordinary_minor, split.preferential_minor) == (6_313 * D, 1_500 * D)
    assert regular_tax(2023, "mfs", 60_000 * D) == 8_513 * D


def test_example_hoh_2025() -> None:
    # taxable 150,000; qualified 10,000 + LTCG 60,000 => ordinary 80,000
    # (midpoint 80,025: 1,700 + 5,742 + 22% * 15,175 = 10,780.50 -> 10,781); 15% * 70,000 = 10,500
    split = estimate_tax_split(
        tax_year=2025,
        filing_status="hoh",
        taxable_income_minor=150_000 * D,
        qualified_dividends_minor=10_000 * D,
        net_ltcg_minor=60_000 * D,
        income_tax_minor=21_281 * D,
    )
    assert split is not None
    assert (split.ordinary_minor, split.preferential_minor) == (10_781 * D, 10_500 * D)
    assert regular_tax(2025, "hoh", 150_000 * D) == 27_108 * D


def test_example_single_2022_twenty_percent_band() -> None:
    # taxable 600,000, LTCG 200,000 => ordinary 400,000 = 113,753 (exact computation worksheet);
    # 15% band: 459,750 - 400,000 = 59,750 -> 8,962.50 -> 8,963; 20% on 140,250 = 28,050.
    # Total 150,766; ordinary 113,753; preferential 37,013.
    split = estimate_tax_split(
        tax_year=2022,
        filing_status="single",
        taxable_income_minor=600_000 * D,
        qualified_dividends_minor=0,
        net_ltcg_minor=200_000 * D,
        income_tax_minor=150_766 * D,
    )
    assert split is not None
    assert (split.ordinary_minor, split.preferential_minor) == (113_753 * D, 37_013 * D)
    assert split.approximate is False


def test_no_preferential_income_means_all_ordinary() -> None:
    tax = regular_tax(2024, "single", 62_000 * D)
    assert tax is not None
    split = estimate_tax_split(
        tax_year=2024,
        filing_status="single",
        taxable_income_minor=62_000 * D,
        qualified_dividends_minor=0,
        net_ltcg_minor=0,
        income_tax_minor=tax,
    )
    assert split is not None
    assert (split.ordinary_minor, split.preferential_minor) == (tax, 0)


def test_net_capital_loss_counts_as_zero_gain() -> None:
    tax = regular_tax(2024, "single", 62_000 * D)
    assert tax is not None
    split = estimate_tax_split(
        tax_year=2024,
        filing_status="single",
        taxable_income_minor=62_000 * D,
        qualified_dividends_minor=0,
        net_ltcg_minor=-3_000 * D,
        income_tax_minor=tax,
    )
    assert split is not None and split.preferential_minor == 0


def test_gains_larger_than_taxable_income_are_clamped() -> None:
    # taxable 30,000 (deductions absorbed part of the gain), LTCG 50,000: whole income is
    # preferential and falls in the 0% band => no tax at all.
    split = estimate_tax_split(
        tax_year=2024,
        filing_status="single",
        taxable_income_minor=30_000 * D,
        qualified_dividends_minor=0,
        net_ltcg_minor=50_000 * D,
        income_tax_minor=0,
    )
    assert split is not None
    assert (split.ordinary_minor, split.preferential_minor) == (0, 0)
    assert split.approximate is False


def test_mismatching_printed_tax_is_flagged_approximate_and_still_sums() -> None:
    split = estimate_tax_split(
        tax_year=2024,
        filing_status="single",
        taxable_income_minor=100_000 * D,
        qualified_dividends_minor=5_000 * D,
        net_ltcg_minor=15_000 * D,
        income_tax_minor=16_559 * D,  # $900 above the worksheet result
    )
    assert split is not None
    assert split.approximate is True
    assert split.ordinary_minor + split.preferential_minor == 16_559 * D


def test_within_two_dollars_is_not_approximate() -> None:
    split = estimate_tax_split(
        tax_year=2024,
        filing_status="single",
        taxable_income_minor=100_000 * D,
        qualified_dividends_minor=5_000 * D,
        net_ltcg_minor=15_000 * D,
        income_tax_minor=15_661 * D,
    )
    assert split is not None and split.approximate is False


# --------------------------------------------------------------------------------------------
# Missing data
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "status"),
    [(2021, "single"), (2027, "single"), (2024, "bogus"), (2024, ""), (2024, "MFJ")],
)
def test_missing_year_or_status_returns_none(year: int, status: str) -> None:
    assert not has_table(year, status)
    assert bracket_tax(year, status, 50_000 * D) is None
    assert regular_tax(year, status, 50_000 * D) is None
    assert (
        worksheet_tax(
            tax_year=year,
            filing_status=status,
            taxable_income_minor=50_000 * D,
            qualified_dividends_minor=0,
            net_ltcg_minor=0,
        )
        is None
    )
    assert (
        estimate_tax_split(
            tax_year=year,
            filing_status=status,
            taxable_income_minor=50_000 * D,
            qualified_dividends_minor=0,
            net_ltcg_minor=0,
            income_tax_minor=6_000 * D,
        )
        is None
    )


def test_negative_inputs_return_none() -> None:
    kwargs: dict[str, Any] = {
        "tax_year": 2024,
        "filing_status": "single",
        "taxable_income_minor": 50_000 * D,
        "qualified_dividends_minor": 0,
        "net_ltcg_minor": 0,
        "income_tax_minor": 6_000 * D,
    }
    for key in ("taxable_income_minor", "qualified_dividends_minor", "income_tax_minor"):
        assert estimate_tax_split(**{**kwargs, key: -1}) is None


def test_2026_worksheet_matches_published_rate_table() -> None:
    # Rev. Proc. 2025-32 Table 3: $192,979.25 + 37% of the excess over $640,600; exact above $100k
    assert bracket_tax(2026, "single", 640_600 * D) == 19_297_925
    assert bracket_tax(2026, "single", 700_000 * D) == 19_297_925 + 59_400 * 37
    assert regular_tax(2026, "single", 105_700 * D) == 17_966 * D  # table: $17,966 at the 24% start
    assert regular_tax(2026, "mfj", 100_800 * D) == 11_600 * D  # table: $11,600 at the 22% start
    assert regular_tax(2026, "hoh", 105_700 * D) == 16_155 * D


def test_rate_lookups() -> None:
    assert tax_calc.marginal_rate_bp(2025, "single", 0) == 1000
    assert tax_calc.marginal_rate_bp(2025, "single", 48_475 * D) == 2200  # boundary is "over"
    assert tax_calc.marginal_rate_bp(2025, "single", 48_474 * D) == 1200
    assert tax_calc.marginal_rate_bp(2025, "single", 10_000_000 * D) == 3700
    assert tax_calc.preferential_rate_bp(2025, "single", 48_350 * D) == 0
    assert tax_calc.preferential_rate_bp(2025, "single", 48_350 * D + 1) == 1500
    assert tax_calc.preferential_rate_bp(2025, "single", 600_000 * D) == 2000
    assert tax_calc.marginal_rate_bp(2021, "single", 0) is None


# --------------------------------------------------------------------------------------------
# Properties (seeded random sweeps)
# --------------------------------------------------------------------------------------------


def _cases(n: int) -> list[tuple[int, str, int, int, int]]:
    rng = random.Random(20240417)
    years = supported_years()
    out = []
    for _ in range(n):
        year = rng.choice(years)
        status = rng.choice(STATUSES)
        taxable = rng.randrange(0, 900_000) * D
        qd = rng.randrange(0, 80_000) * D if rng.random() < 0.6 else 0
        ltcg = rng.randrange(-5_000, 400_000) * D if rng.random() < 0.7 else 0
        out.append((year, status, taxable, qd, ltcg))
    return out


def test_properties_over_random_inputs() -> None:
    for year, status, taxable, qd, ltcg in _cases(600):
        printed = worksheet_tax(
            tax_year=year,
            filing_status=status,
            taxable_income_minor=taxable,
            qualified_dividends_minor=qd,
            net_ltcg_minor=ltcg,
        )
        assert printed is not None
        regular = regular_tax(year, status, taxable)
        assert regular is not None
        # preferential treatment never raises tax
        assert printed <= regular
        split = estimate_tax_split(
            tax_year=year,
            filing_status=status,
            taxable_income_minor=taxable,
            qualified_dividends_minor=qd,
            net_ltcg_minor=ltcg,
            income_tax_minor=printed,
        )
        assert split is not None
        assert split.ordinary_minor >= 0 and split.preferential_minor >= 0
        assert split.ordinary_minor + split.preferential_minor == printed
        assert split.approximate is False
        pref_amount = min(taxable, qd + max(ltcg, 0))
        if pref_amount == 0:
            assert split.preferential_minor == 0
            assert split.ordinary_minor == regular
        assert split.ordinary_minor <= regular


def test_worksheet_tax_is_monotonic_in_taxable_income() -> None:
    for status in STATUSES:
        previous = -1
        for taxable in range(0, 700_000, 997):
            tax = worksheet_tax(
                tax_year=2024,
                filing_status=status,
                taxable_income_minor=taxable * D,
                qualified_dividends_minor=3_000 * D,
                net_ltcg_minor=20_000 * D,
            )
            assert tax is not None and tax >= previous
            previous = tax


def test_qss_equals_mfj_everywhere() -> None:
    for year, _, taxable, qd, ltcg in _cases(100):
        args: dict[str, Any] = {
            "tax_year": year,
            "taxable_income_minor": taxable,
            "qualified_dividends_minor": qd,
            "net_ltcg_minor": ltcg,
        }
        assert worksheet_tax(filing_status="qss", **args) == worksheet_tax(
            filing_status="mfj", **args
        )
