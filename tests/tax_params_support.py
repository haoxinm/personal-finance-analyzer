"""Helpers that build tax-parameter files for tests (the shipped file plus a plausible next year)."""

from __future__ import annotations

import copy
from importlib import resources
from typing import Any

import yaml


def shipped_raw() -> dict[str, Any]:
    text = (
        resources.files("spend_analyzer.core")
        .joinpath("tax_data", "federal_params.yaml")
        .read_text(encoding="utf-8")
    )
    raw: dict[str, Any] = yaml.safe_load(text)
    return raw


def grown_year(previous: dict[str, Any], *, pct: int = 4) -> dict[str, Any]:
    """A copy of one year's raw block with every indexed amount raised by `pct` percent (rounded
    to whole dollars), statutory amounts unchanged: what a real next year looks like."""

    def up(value: int) -> int:
        return round(value * (100 + pct) / 100)

    year = copy.deepcopy(previous)
    year["sources"] = ["Rev. Proc. 2026-99 (test)"]
    for key in ("ss_wage_base", "elective_deferral_limit"):
        year[key] = up(year[key])
    for key in ("standard_deduction", "additional_standard_deduction"):
        year[key] = {status: up(v) for status, v in year[key].items()}
    year["brackets"] = {
        status: [[0 if t == 0 else up(t), rate] for t, rate in rows]
        for status, rows in year["brackets"].items()
    }
    year["ltcg"] = {status: [up(a), up(b)] for status, (a, b) in year["ltcg"].items()}
    return year


def with_next_year(version: int, *, year: int = 2027, pct: int = 4) -> dict[str, Any]:
    """The shipped file with a plausible `year` (default 2027) added."""
    raw = shipped_raw()
    raw["params_version"] = version
    raw["years"][year] = grown_year(raw["years"][year - 1], pct=pct)
    return raw
