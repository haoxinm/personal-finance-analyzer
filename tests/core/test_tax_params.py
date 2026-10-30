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
    load_bundled,
    parse_params,
    parse_params_text,
)

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
        assert all(s.startswith("Rev. Proc. 20") for s in params.sources), year


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


def _wage_base_2026() -> int:
    p = tax_calc.params_for(2026)
    assert p is not None
    return p.ss_wage_base_minor // D


def test_bundled_is_used_without_optional_files(home: Path) -> None:
    assert tax_params.resolve().source == "bundled"
    assert _wage_base_2026() == 184_500


def test_cached_download_wins_only_when_newer(home: Path) -> None:
    bundled = load_bundled().version
    tax_params.install_cached(_with_version(bundled + 1, wage_base_2026=190_000))
    assert tax_params.resolve().source == "cached"
    assert _wage_base_2026() == 190_000  # picked up without a restart
    tax_params.install_cached(_with_version(bundled, wage_base_2026=191_000))
    assert tax_params.resolve().source == "bundled"  # a tie goes to the bundled file
    assert _wage_base_2026() == 184_500


def test_override_wins_over_a_newer_cached_copy(home: Path) -> None:
    bundled = load_bundled().version
    tax_params.install_cached(_with_version(bundled + 5, wage_base_2026=190_000))
    tax_params.install_override(_with_version(1, wage_base_2026=185_000))
    assert tax_params.resolve().source == "override"
    assert _wage_base_2026() == 185_000
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
