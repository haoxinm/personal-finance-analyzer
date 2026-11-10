"""Preferential-vs-ordinary federal income tax estimate (plan 2.6).

The bracket data and the Qualified Dividends and Capital Gain Tax Worksheet moved to
`core/tax_calc.py` (parameters in `core/tax_data/federal_params.yaml`) so the per-user estimate in
`analytics/` can use them without importing from `ingest/`. This module keeps the names the 1040
parser and its tests use: see `core/tax_calc.py` for units, rounding and limits.
"""

from __future__ import annotations

from spend_analyzer.core.tax_calc import (
    APPROXIMATE_TOLERANCE_MINOR,
    FILING_STATUSES,
    TaxSplit,
    bracket_tax,
    estimate_tax_split,
    has_table,
    regular_tax,
    supported_years,
    worksheet_tax,
)

__all__ = [
    "APPROXIMATE_TOLERANCE_MINOR",
    "FILING_STATUSES",
    "TaxSplit",
    "bracket_tax",
    "estimate_tax_split",
    "has_table",
    "regular_tax",
    "supported_years",
    "worksheet_tax",
]
