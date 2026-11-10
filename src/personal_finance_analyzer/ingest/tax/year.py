"""Tax-year detection from form text.

`detect_tax_year(doc)` looks for the year in the places tax forms print it: ``Tax Year 2024``,
``calendar year 2024``, ``Form 1040 (2024)``, ``2024 Form W-2``, ``Wage and Tax Statement 2024``.
Strong, labelled patterns win over weaker ones; within a tier the most frequent year wins (ties
go to the first occurrence). Revision stamps such as ``(Rev. January 2024)`` are never matched.

Helper module: exposes no parser-shaped objects.
"""

from __future__ import annotations

import re
from collections import Counter

from personal_finance_analyzer.core.types import ExtractedDoc

_YEAR = r"(20\d{2})"
_MIN_YEAR = 2000
_MAX_YEAR = 2099

#: Tiers, strongest first. Each pattern captures the year in group 1.
_TIERS: tuple[tuple[re.Pattern[str], ...], ...] = (
    (
        re.compile(
            rf"\b(?:tax|calendar)\s+year\s*[:\-]?\s*(?:ending\s+)?(?:dec(?:ember)?\.?\s*31,?\s*)?{_YEAR}\b",
            re.I,
        ),
        re.compile(
            rf"\bfor\s+(?:the\s+)?(?:calendar\s+)?year\s+(?:ending\s+)?(?:dec(?:ember)?\.?\s*31,?\s*)?{_YEAR}\b",
            re.I,
        ),
        re.compile(
            rf"\bjan(?:uary)?\.?\s*1\s*[-\u2013 ]+\s*(?:to\s+)?dec(?:ember)?\.?\s*31,?\s*{_YEAR}\b",
            re.I,
        ),
    ),
    (
        re.compile(rf"\b{_YEAR}\s+form\s+(?:w-?2|1099|1040)\b", re.I),
        re.compile(
            rf"\bform\s+(?:w-?2|1099(?:-[a-z]{{1,4}})?|1040(?:-[a-z]{{1,2}})?)\s*\(?\s*{_YEAR}\b",
            re.I,
        ),
        re.compile(rf"\bwage\s+and\s+tax\s+statement\s*[:\-\u2013]?\s*{_YEAR}\b", re.I),
        re.compile(rf"\bindividual\s+income\s+tax\s+return\s*[:\-\u2013]?\s*{_YEAR}\b", re.I),
    ),
)


def detect_tax_year(doc: ExtractedDoc) -> int | None:
    """Return the tax year printed on `doc`, or `None` when none is found.

    Only years 2000-2099 are considered. Never raises.
    """
    text = doc.full_text
    for tier in _TIERS:
        counts: Counter[int] = Counter()
        first_seen: dict[int, int] = {}
        for pattern in tier:
            for match in pattern.finditer(text):
                year = int(match.group(1))
                if not _MIN_YEAR <= year <= _MAX_YEAR:
                    continue
                counts[year] += 1
                first_seen[year] = min(first_seen.get(year, match.start()), match.start())
        if counts:
            return min(counts, key=lambda y: (-counts[y], first_seen[y]))
    return None
