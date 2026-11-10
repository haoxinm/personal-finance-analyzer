from __future__ import annotations

import pytest

from personal_finance_analyzer.ingest.tax.year import detect_tax_year
from tests.ingest.tax_support import doc_from_lines


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["Form 1040 U.S. Individual Income Tax Return", "Tax Year 2024"], 2024),
        (["For calendar year 2023"], 2023),
        (["Form 1040 (2022)"], 2022),
        (["2024 Form W-2 Wage and Tax Statement"], 2024),
        (["W-2 Wage and Tax Statement 2021"], 2021),
        (["Form 1099-INT  (Rev. January 2023)", "Interest Income"], None),
        (["Form 1099-DIV 2020 Dividends"], 2020),
        (["For the year January 1 - December 31, 2019"], 2019),
        (["Statement date 2024", "Nothing else"], None),
        ([], None),
    ],
)
def test_detect_tax_year(lines: list[str], expected: int | None) -> None:
    assert detect_tax_year(doc_from_lines(lines)) == expected


def test_strong_tier_beats_weak_and_majority_wins() -> None:
    doc = doc_from_lines(
        ["Form 1099-INT (2023)", "Tax Year 2024", "Tax year 2024", "Form 1040 (2023)"]
    )
    assert detect_tax_year(doc) == 2024


def test_implausible_year_is_ignored() -> None:
    assert detect_tax_year(doc_from_lines(["Tax Year 1999"])) is None
