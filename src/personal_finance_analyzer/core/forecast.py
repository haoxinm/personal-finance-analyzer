"""Net worth forecast vocabulary: parameter bounds and the built-in presets.

Pure data: no I/O, no database, no network, and no LLM. The projection itself is computed in the
browser (`frontend/src/screens/net-worth/forecast/forecast-logic.ts`) so it recomputes instantly as
a slider moves; the server only supplies history and stores presets.

Rates are percentages per year (3.0 means 3%), amounts are integer USD minor units (cents), and
years are calendar years. The built-in presets are illustrative assumptions, not predictions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Longest projection horizon (years after the base year).
MAX_HORIZON_YEARS: Final = 60
#: Largest accepted amount (minor units): keeps sums over a 60-year horizon far inside int64.
MAX_AMOUNT_MINOR: Final = 10**13
#: Calendar-year bounds shared by history years, event years and the retirement year.
MIN_YEAR: Final = 1900
MAX_YEAR: Final = 2300
MAX_EVENTS: Final = 50


@dataclass(frozen=True)
class BuiltinPreset:
    """A read-only preset. Applying one sets only the four rates below; the rest of the scenario
    (starting values, retirement, one-off events, target year) is left as the user has it."""

    key: str
    name: str
    description: str
    income_growth_pct: float
    return_pct: float
    spending_growth_pct: float
    inflation_pct: float


BUILTIN_PRESETS: Final[tuple[BuiltinPreset, ...]] = (
    BuiltinPreset(
        key="conservative",
        name="Conservative",
        description="Income +2%/yr, 4% investment return, spending +3%/yr, 3% inflation.",
        income_growth_pct=2.0,
        return_pct=4.0,
        spending_growth_pct=3.0,
        inflation_pct=3.0,
    ),
    BuiltinPreset(
        key="moderate",
        name="Moderate",
        description="Income +3%/yr, 6% investment return, spending +2.5%/yr, 2.5% inflation.",
        income_growth_pct=3.0,
        return_pct=6.0,
        spending_growth_pct=2.5,
        inflation_pct=2.5,
    ),
    BuiltinPreset(
        key="aggressive",
        name="Aggressive",
        description="Income +5%/yr, 8% investment return, spending +2%/yr, 2.5% inflation.",
        income_growth_pct=5.0,
        return_pct=8.0,
        spending_growth_pct=2.0,
        inflation_pct=2.5,
    ),
)


def normalize_preset_name(raw: str) -> str:
    """Strip and collapse internal whitespace; reject an empty result.

    Raises:
        ValueError: the name is empty after normalisation.
    """
    name = " ".join(raw.split())
    if not name:
        raise ValueError("preset name must not be empty")
    return name


def is_builtin_name(name: str) -> bool:
    """True when `name` equals a built-in preset's name, ignoring case."""
    return name.casefold() in {p.name.casefold() for p in BUILTIN_PRESETS}
