"""Tests for `core.tax_params`: the params file schema, and which file wins (bundled, downloaded
copy, user override)."""

from __future__ import annotations

import copy
import stat
import sys
from datetime import date
from importlib import resources
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
import yaml

from spend_analyzer.core import tax_calc, tax_params
from spend_analyzer.core.tax_params import (
    FILING_STATUSES,
    added_years,
    load_bundled,
    parse_params,
    parse_params_text,
)
from tests.tax_params_support import grown_year, with_next_year

D = 100


def _bundled_raw() -> dict[str, Any]:
    text = (
        resources.files("spend_analyzer.core")
        .joinpath("tax_data", "federal_params.yaml")
        .read_text(encoding="utf-8")
    )
    raw: dict[str, Any] = yaml.safe_load(text)
    return raw


def _text(raw: dict[str, Any]) -> str:
    return yaml.safe_dump(raw)


# --- the shipped file ------------------------------------------------------------------------------


def test_bundled_file_validates_and_covers_2022_to_2026() -> None:
    bundled = load_bundled()
    assert bundled.source == "bundled"
    assert bundled.version >= 1
    assert sorted(bundled.years) == [2022, 2023, 2024, 2025, 2026]


def test_every_year_cites_a_revenue_procedure() -> None:
    for year, params in load_bundled().years.items():
        assert params.sources, year
        assert params.sources[0].startswith("Rev. Proc. 20"), year
        assert all(s.startswith(("Rev. Proc. 20", "IRC 151(d)(5)(C)")) for s in params.sources), (
            year
        )


def test_every_year_has_every_filing_status() -> None:
    for year, params in load_bundled().years.items():
        for mapping in (
            params.standard_deduction_minor,
            params.niit_threshold_minor,
            params.additional_medicare_threshold_minor,
            params.tables,
        ):
            assert set(mapping) == set(FILING_STATUSES), year


def test_thresholds_increase_year_over_year() -> None:
    years = load_bundled().years
    ordered = sorted(years)
    for a, b in pairwise(ordered):
        pa, pb = years[a], years[b]
        assert pb.ss_wage_base_minor > pa.ss_wage_base_minor
        assert pb.elective_deferral_limit_minor > pa.elective_deferral_limit_minor
        for status in FILING_STATUSES:
            assert pb.standard_deduction_minor[status] > pa.standard_deduction_minor[status]
        assert (
            pb.additional_standard_deduction_married_minor
            > pa.additional_standard_deduction_married_minor
        )


@pytest.mark.parametrize(
    ("year", "single", "mfj", "hoh", "add_married", "add_unmarried", "wage_base", "k401"),
    [  # copied from the revenue procedures' text, the SSA table and the IRS announcements
        (2022, 12_950, 25_900, 19_400, 1_400, 1_750, 147_000, 20_500),
        (2023, 13_850, 27_700, 20_800, 1_500, 1_850, 160_200, 22_500),
        (2024, 14_600, 29_200, 21_900, 1_550, 1_950, 168_600, 23_000),
        (2025, 15_750, 31_500, 23_625, 1_600, 2_000, 176_100, 23_500),  # post-OBBBA
        (2026, 16_100, 32_200, 24_150, 1_650, 2_050, 184_500, 24_500),
    ],
)
def test_published_deductions_and_limits(
    year: int,
    single: int,
    mfj: int,
    hoh: int,
    add_married: int,
    add_unmarried: int,
    wage_base: int,
    k401: int,
) -> None:
    p = load_bundled().years[year]
    assert p.standard_deduction_minor == {
        "single": single * D,
        "mfj": mfj * D,
        "mfs": single * D,
        "hoh": hoh * D,
        "qss": mfj * D,
    }
    assert p.additional_standard_deduction_married_minor == add_married * D
    assert p.additional_standard_deduction_unmarried_minor == add_unmarried * D
    assert p.ss_wage_base_minor == wage_base * D
    assert p.elective_deferral_limit_minor == k401 * D
    assert p.capital_loss_limit_minor == 3_000 * D
    assert p.capital_loss_limit_mfs_minor == 1_500 * D
    assert p.niit_threshold_minor["mfj"] == 250_000 * D
    assert p.additional_medicare_threshold_minor["mfs"] == 125_000 * D


def test_standard_deduction_adds_the_aged_blind_amount() -> None:
    p = load_bundled().years[2025]
    assert p.standard_deduction("single", 1) == (15_750 + 2_000) * D
    assert p.standard_deduction("mfj", 2) == (31_500 + 2 * 1_600) * D
    assert p.standard_deduction("qss", 1) == (31_500 + 1_600) * D  # a surviving spouse: married
    assert p.standard_deduction("hoh", 0) == 23_625 * D
    assert p.capital_loss_limit("mfs") == 1_500 * D
    assert p.capital_loss_limit("single") == 3_000 * D


# --- schema validation -----------------------------------------------------------------------------


def _mutated(mutate: Any) -> dict[str, Any]:
    raw = copy.deepcopy(_bundled_raw())
    mutate(raw)
    return raw


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r.pop("params_version"), "params_version"),
        (lambda r: r.update(params_version=0), "params_version"),
        (lambda r: r.update(params_version=True), "params_version"),
        (lambda r: r.pop("years"), "years"),
        (lambda r: r["years"][2024].pop("sources"), "sources"),
        (lambda r: r["years"][2024].update(sources=[]), "sources"),
        (lambda r: r["years"][2024]["standard_deduction"].pop("hoh"), "exactly"),
        (lambda r: r["years"][2024]["brackets"].pop("mfs"), "exactly"),
        (lambda r: r["years"][2024]["brackets"]["single"].reverse(), "increasing|first bracket"),
        (lambda r: r["years"][2024]["brackets"]["single"][2].__setitem__(0, 5), "increasing"),
        (lambda r: r["years"][2024]["brackets"]["single"][1].__setitem__(1, 99), "increasing"),
        (lambda r: r["years"][2024]["ltcg"].__setitem__("single", [500_000, 400_000]), "0% band"),
        (lambda r: r["years"][2024]["ltcg"].__setitem__("qss", [1, 2]), "qss"),
        (lambda r: r["years"][2024].update(ss_wage_base=1.5), "ss_wage_base"),
        (lambda r: r["years"][2024].update(ss_wage_base=-1), "ss_wage_base"),
        (lambda r: r["years"].update({"abc": r["years"][2024]}), "invalid tax year"),
        (lambda r: r.update(years={}), "no tax years"),
    ],
)
def test_schema_rejects(mutate: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_params(_mutated(mutate))


def test_schema_rejects_non_mapping_and_oversized_and_bad_yaml() -> None:
    with pytest.raises(ValueError):
        parse_params([1, 2])
    with pytest.raises(ValueError, match="too large"):
        parse_params_text("#" * (tax_params.MAX_PARAMS_BYTES + 1))
    with pytest.raises(ValueError, match="not valid YAML"):
        parse_params_text("a: [unterminated")


# --- precedence ------------------------------------------------------------------------------------


def _with_version(version: int, *, wage_base_2026: int | None = None) -> str:
    raw = _bundled_raw()
    raw["params_version"] = version
    if wage_base_2026 is not None:
        raw["years"][2026]["ss_wage_base"] = wage_base_2026
    return _text(raw)


def _wage_base(year: int) -> int:
    p = tax_calc.params_for(year)
    assert p is not None
    return p.ss_wage_base_minor // D


def test_bundled_is_used_without_optional_files(home: Path) -> None:
    assert tax_params.resolve().source == "bundled"
    assert _wage_base(2026) == 184_500


def test_download_adds_a_new_year_and_never_replaces_a_bundled_one(home: Path) -> None:
    raw = with_next_year(load_bundled().version + 1)
    raw["years"][2026]["ss_wage_base"] = 190_000  # a "correction" of a bundled year: ignored
    tax_params.install_cached(_text(raw))
    resolved = tax_params.resolve()
    assert resolved.source == "cached"
    assert sorted(resolved.years) == [2022, 2023, 2024, 2025, 2026, 2027]
    assert _wage_base(2026) == 184_500  # the app's figure wins
    assert _wage_base(2027) == round(184_500 * 1.04)


def test_download_cannot_drop_years(home: Path) -> None:
    raw = with_next_year(load_bundled().version + 9)
    for year in (2022, 2023, 2024):
        del raw["years"][year]
    tax_params.install_cached(_text(raw))
    assert sorted(tax_params.resolve().years) == [2022, 2023, 2024, 2025, 2026, 2027]


def test_cached_copy_without_new_years_changes_nothing(home: Path) -> None:
    tax_params.install_cached(_with_version(load_bundled().version + 1, wage_base_2026=191_000))
    assert tax_params.resolve().source == "bundled"
    assert _wage_base(2026) == 184_500


def test_implausible_cached_file_is_ignored_as_a_whole(home: Path) -> None:
    raw = with_next_year(load_bundled().version + 1)
    raw["years"][2027]["standard_deduction"]["single"] *= 2
    raw["years"][2027]["standard_deduction"]["mfs"] *= 2
    tax_params.install_cached(_text(raw))  # structurally valid, so it installs...
    assert tax_params.resolve().source == "bundled"  # ...but resolve re-checks it
    assert 2027 not in tax_params.resolve().years


def test_override_replaces_listed_years_and_keeps_the_rest(home: Path) -> None:
    bundled = load_bundled().version
    tax_params.install_cached(_text(with_next_year(bundled + 5)))
    tax_params.install_override(_with_version(1, wage_base_2026=185_000))
    resolved = tax_params.resolve()
    assert resolved.source == "override"
    assert _wage_base(2026) == 185_000
    assert _wage_base(2027) > 0  # the cached addition survives an override that lacks 2027
    assert tax_params.clear_override() is True
    assert tax_params.clear_override() is False
    assert tax_params.resolve().source == "cached"


def test_invalid_optional_files_are_ignored(home: Path) -> None:
    tax_params.tax_params_dir().mkdir(parents=True)
    tax_params.cached_path().write_text("not: [valid", encoding="utf-8")
    tax_params.override_path().write_text(
        _text({"params_version": 9, "years": {2024: {}}}), encoding="utf-8"
    )
    assert tax_params.resolve().source == "bundled"
    assert tax_params.versions() == {
        "bundled": load_bundled().version,
        "cached": None,
        "override": None,
    }


def test_install_rejects_invalid_text_and_leaves_no_file(home: Path) -> None:
    with pytest.raises(ValueError):
        tax_params.install_cached("params_version: 3\nyears: {}\n")
    with pytest.raises(ValueError):
        tax_params.install_override("nonsense")
    assert not tax_params.cached_path().exists()
    assert not tax_params.override_path().exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_files_are_written_private(home: Path) -> None:
    tax_params.install_override(_with_version(2))
    tax_params.record_check(date(2026, 1, 2), "current")
    for path in (tax_params.override_path(), tax_params.tax_params_dir() / "state.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_check_state_round_trip_and_corruption(home: Path) -> None:
    assert tax_params.read_check_state() == tax_params.CheckState()
    tax_params.record_check(date(2026, 3, 4), "updated")
    state = tax_params.read_check_state()
    assert (state.last_checked, state.outcome) == (date(2026, 3, 4), "updated")
    (tax_params.tax_params_dir() / "state.json").write_text("{broken", encoding="utf-8")
    assert tax_params.read_check_state() == tax_params.CheckState()


# --- sanity checks on downloaded years -------------------------------------------------------------


def _candidate(mutate: Any, year: int = 2027) -> tax_params.ParamSet:
    raw = with_next_year(5, year=year)
    mutate(raw["years"][year])
    return parse_params(raw, source="cached")


def test_a_plausible_next_year_is_accepted() -> None:
    added = added_years(load_bundled().years, _candidate(lambda y: None))
    assert sorted(added) == [2027]
    assert added[2027].senior_deduction_minor == 6_000 * D  # statutory amount carried over


def test_bundled_years_pass_their_own_year_over_year_checks() -> None:
    years = load_bundled().years
    for year in sorted(years)[1:]:
        if year == 2025:
            continue  # the senior deduction begins in 2025 (a download may not begin one)
        tax_params._check_year(years[year], years[year - 1])


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda y: y["standard_deduction"].update(single=40_000, mfs=40_000), "15%"),
        (lambda y: y.update(ss_wage_base=300_000), "15%"),
        (lambda y: y.update(ss_wage_base=1), "plausible|15%"),
        (lambda y: y.update(elective_deferral_limit=50_000), "15%"),
        (lambda y: y["additional_standard_deduction"].update(unmarried=9_000), "15%"),
        (lambda y: y["brackets"]["single"][6].__setitem__(0, 800_000), "15%"),
        (lambda y: y["ltcg"].update(single=[1_000_000, 2_000_000]), "15%|increasing|0% band"),
        (lambda y: y["brackets"]["single"][1].__setitem__(1, 11), "rates differ"),
        (lambda y: y["brackets"]["single"].pop(), "rates differ|zip"),
        (lambda y: y["capital_loss_limit"].update(default=4_000), "not indexed"),
        (lambda y: y["niit_threshold"].update(single=250_000), "not indexed"),
        (lambda y: y["additional_medicare_threshold"].update(mfj=300_000), "not indexed"),
        (lambda y: y["senior_deduction"].update(amount=7_000), "senior_deduction"),
        (lambda y: y["standard_deduction"].update(hoh=40_000), "ordered|15%"),
    ],
)
def test_implausible_added_year_is_rejected(mutate: Any, message: str) -> None:
    def both(y: dict[str, Any]) -> None:
        mutate(y)
        # keep qss tied to mfj so only the sanity check (not the schema) can object
        y["standard_deduction"]["qss"] = y["standard_deduction"]["mfj"]
        y["brackets"]["qss"] = y["brackets"]["mfj"]
        y["ltcg"]["qss"] = y["ltcg"]["mfj"]

    with pytest.raises(ValueError, match=message):
        added_years(load_bundled().years, _candidate(both))


def test_senior_deduction_may_end_but_not_begin() -> None:
    known = dict(load_bundled().years)
    raw = with_next_year(5, year=2027)
    raw["years"][2028] = grown_year(raw["years"][2027])
    raw["years"][2029] = grown_year(raw["years"][2028])
    del raw["years"][2029]["senior_deduction"]  # the deduction expires after 2028
    added = added_years(known, parse_params(raw, source="cached"), latest_year=2029)
    assert added[2029].senior_deduction_minor == 0
    raw["years"][2030] = grown_year(raw["years"][2029])
    raw["years"][2030]["senior_deduction"] = raw["years"][2028]["senior_deduction"]
    with pytest.raises(ValueError, match="senior_deduction"):
        added_years(known, parse_params(raw, source="cached"), latest_year=2030)


def test_a_year_needs_a_known_predecessor_and_cannot_be_far_ahead() -> None:
    raw = with_next_year(5, year=2027)
    raw["years"][2029] = grown_year(raw["years"][2027])
    with pytest.raises(ValueError, match="2028"):
        added_years(load_bundled().years, parse_params(raw, source="cached"), latest_year=2030)
    with pytest.raises(ValueError, match="far ahead"):
        added_years(load_bundled().years, parse_params(raw, source="cached"), latest_year=2026)


def test_existing_years_in_a_download_are_ignored_even_if_different() -> None:
    raw = with_next_year(5)
    raw["years"][2024]["ss_wage_base"] = 999_999
    added = added_years(load_bundled().years, parse_params(raw, source="cached"))
    assert sorted(added) == [2027]


# --- the 2025-2028 senior deduction ----------------------------------------------------------------


def test_senior_deduction_params_and_phaseout() -> None:
    years = load_bundled().years
    assert years[2024].senior_deduction_minor == 0
    assert years[2024].senior_deduction("single", 1, 0) == 0
    p = years[2025]
    assert p.senior_deduction("single", 1, 50_000 * D) == 6_000 * D
    assert p.senior_deduction("single", 1, 95_000 * D) == 4_800 * D  # 6% of 20,000 = 1,200
    assert p.senior_deduction("single", 1, 175_000 * D) == 0  # fully phased out at 175,000
    assert p.senior_deduction("mfj", 2, 150_000 * D) == 12_000 * D
    assert p.senior_deduction("mfj", 2, 190_000 * D) == (12_000 - 2_400) * D
    assert p.senior_deduction("mfj", 1, 100_000 * D) == 6_000 * D
    assert p.senior_deduction("mfj", 3, 100_000 * D) == 12_000 * D  # at most two on a joint return
    assert p.senior_deduction("single", 2, 0) == 6_000 * D  # one taxpayer
    assert p.senior_deduction("mfs", 1, 0) == 0  # a married filer must file jointly
    assert p.senior_deduction("qss", 1, 70_000 * D) == 6_000 * D
    assert p.senior_deduction("hoh", 0, 0) == 0
    assert years[2026].senior_deduction_minor == 6_000 * D


def test_senior_block_schema_rejects() -> None:
    def break_it(mutate: Any) -> dict[str, Any]:
        return _mutated(lambda r: mutate(r["years"][2025]["senior_deduction"]))

    with pytest.raises(ValueError, match="phaseout_rate_percent"):
        parse_params(break_it(lambda b: b.update(phaseout_rate_percent=0)))
    with pytest.raises(ValueError, match="exactly"):
        parse_params(break_it(lambda b: b["phaseout_threshold"].update(mfs=75_000)))
    with pytest.raises(ValueError, match="amount"):
        parse_params(break_it(lambda b: b.pop("amount")))
