"""Shared pieces for the synthetic tax-form fixture generators (WP3).

Provides (1) invented identities, (2) a deterministic box-layout PDF renderer plus helpers that
lay out IRS-style cells (a small label line with the value on the line beneath it, inside the same
x-range — this is what exercises `fields.amount_for_label(mode="below")`), and (3) golden-dict
helpers.

Software-style layouts (label ... value on one row; 1040, consolidated 1099) use
`tests.fixtures.gen.base.render_lines_pdf` + `format_row` instead.

This module deliberately defines NO builder-shaped object (`layout_id` + `variants` + `build`):
`tests/generate_fixtures.py` scans every module in this package and would pick it up.

Everything here is INVENTED. SSNs use the never-issued 9xx area; EINs use the unassigned 99
prefix; names are fictional (hygiene enforced by `tests/fixtures/test_tax_fixture_hygiene.py`).

Developer API
-------------
    Entity(name, ein, address_lines)                        frozen dataclass
    RECIPIENT_NAME, RECIPIENT_SSN, SPOUSE_NAME, SPOUSE_SSN, RECIPIENT_ADDRESS_LINES
    EMPLOYER_A, EMPLOYER_B, BANK, BROKER                    Entity instances
    CREDIT_UNION, BROKER_B                                  a second bank / broker
    ACCOUNT_NUMBER, ACCOUNT_NUMBER_2                        "SYN-000123456789", "SYN-000555000111"
    last4(identifier) -> str

    BoxItem = tuple[float, float, str]                      (x, baseline y from page top, text)
    render_boxes_pdf(path, pages: Sequence[Sequence[BoxItem]], *, font_size=7.0, page_size=letter)
    cell(x, y, label, value, *, width=None, value_dy=VALUE_DY, font_size=7.0) -> list[BoxItem]
    grid(x0, y0, rows: Sequence[Sequence[tuple[str, str]]], *, col_width, row_height=...) -> list[BoxItem]
    text_item(x, y, text) -> list[BoxItem]                  a lone line (titles, headers)
    golden_dict(doc: ParsedTaxDocument) -> dict             JSON-able, key-sorted-stable
    build_golden(doc, *, expected_warnings=()) -> dict      golden_dict + "expected_warnings"
    money(dollars: str) -> int                              "1,234.56" -> 123456 (Decimal, no float)

Layout rules: Courier advances 0.6 * font_size per character (4.2 pt at 7 pt), so `cell`/`grid`
raise ValueError when a label or value would be wider than its cell (cells must not bleed into
the neighbour's x-range, otherwise "below" extraction becomes ambiguous). Rows closer than ~5 pt
merge into one visual row in `cluster_rows`; `VALUE_DY` (10 pt) keeps label and value rows apart.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from personal_finance_analyzer.core.tax_types import ParsedTaxDocument

# --------------------------------------------------------------------------------------------
# Invented identities
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entity:
    """An invented payer: company name, invented EIN (99-xxxxxxx) and a fake address."""

    name: str
    ein: str
    address_lines: tuple[str, ...]


RECIPIENT_NAME = "Jordan Q. Testperson"
RECIPIENT_SSN = "912-34-5678"  # 9xx area: never issued
SPOUSE_NAME = "Riley M. Testperson"
SPOUSE_SSN = "923-45-6789"  # 9xx area: never issued
RECIPIENT_ADDRESS_LINES = ("100 Example Lane", "Testville, ZZ 00000")

EMPLOYER_A = Entity(
    "Pinecrest Logistics LLC", "99-0000001", ("200 Sample Road", "Testville, ZZ 00000")
)
EMPLOYER_B = Entity(
    "Alder Ridge Brewing Co", "99-0000002", ("300 Mock Street", "Exampleton, ZZ 00001")
)
BANK = Entity("Harbor Mill Savings", "99-0000003", ("400 Fictional Ave", "Testville, ZZ 00000"))
BROKER = Entity(
    "Copperfield Securities", "99-0000004", ("500 Placeholder Plaza", "Exampleton, ZZ 00001")
)

CREDIT_UNION = Entity(
    "Willow Bend Credit Union", "99-0000005", ("600 Sample Court", "Testville, ZZ 00000")
)
BROKER_B = Entity(
    "Larkspur Investment Services", "99-0000006", ("700 Mock Terrace", "Exampleton, ZZ 00001")
)

ACCOUNT_NUMBER = "SYN-000123456789"
ACCOUNT_NUMBER_2 = "SYN-000555000111"


def last4(identifier: str) -> str:
    """Last four digits of an identifier, e.g. ``last4("99-0000001") == "0001"``."""
    digits = "".join(ch for ch in identifier if ch.isdigit())
    return digits[-4:]


def money(dollars: str) -> int:
    """Minor units from a printed amount (``"1,234.56"`` -> ``123456``); Decimal, never float."""
    return int((Decimal(dollars.replace(",", "")) * 100).to_integral_value(rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------------------------
# Box-layout renderer
# --------------------------------------------------------------------------------------------

BoxItem = tuple[float, float, str]

DEFAULT_FONT = "Courier"
DEFAULT_BOX_FONT_SIZE = 7.0
#: Vertical distance from a cell's label baseline to its value baseline.
VALUE_DY = 10.0
#: Default distance between rows of cells in `grid`.
DEFAULT_ROW_HEIGHT = 26.0
_CHAR_ADVANCE = 0.6  # Courier: advance = 0.6 * font size


def render_boxes_pdf(
    path: Path,
    pages: Sequence[Sequence[BoxItem]],
    *,
    font_name: str = DEFAULT_FONT,
    font_size: float = DEFAULT_BOX_FONT_SIZE,
    page_size: tuple[float, float] = letter,
) -> None:
    """Render absolutely positioned text to a deterministic PDF (reportlab, ``invariant=1`` so two
    renders are byte-identical). Each page is a sequence of ``(x, y_from_top, text)``: `x` is the
    left edge and `y_from_top` the text baseline measured down from the top of the page, in
    points. Empty text is skipped."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=page_size, invariant=1)
    _, page_height = page_size
    for items in pages:
        pdf.setFont(font_name, font_size)
        for x, y_from_top, text in items:
            if text:
                pdf.drawString(x, page_height - y_from_top, text)
        pdf.showPage()
    pdf.save()


def _fits(text: str, width: float, font_size: float, what: str) -> None:
    needed = len(text) * _CHAR_ADVANCE * font_size
    if needed > width:
        raise ValueError(f"{what} is {needed:.1f}pt wide but its cell is only {width:.1f}pt")


def text_item(x: float, y: float, text: str) -> list[BoxItem]:
    """A single free-standing line (form title, header)."""
    return [(x, y, text)]


def cell(
    x: float,
    y: float,
    label: str,
    value: str,
    *,
    width: float | None = None,
    value_dy: float = VALUE_DY,
    font_size: float = DEFAULT_BOX_FONT_SIZE,
) -> list[BoxItem]:
    """One IRS-style cell: `label` at ``(x, y)`` and `value` directly beneath it at
    ``(x, y + value_dy)``, both left-aligned on `x` so they share an x-range. An empty `value`
    yields a blank box (label only). With `width` set, raises `ValueError` if either string is
    wider than the cell."""
    if width is not None:
        _fits(label, width, font_size, "label")
        _fits(value, width, font_size, "value")
    items: list[BoxItem] = [(x, y, label)]
    if value:
        items.append((x, y + value_dy, value))
    return items


def grid(
    x0: float,
    y0: float,
    rows: Sequence[Sequence[tuple[str, str]]],
    *,
    col_width: float,
    row_height: float = DEFAULT_ROW_HEIGHT,
    font_size: float = DEFAULT_BOX_FONT_SIZE,
) -> list[BoxItem]:
    """Lay out `rows` of ``(label, value)`` cells on a regular grid: row ``r`` at
    ``y0 + r * row_height``, column ``c`` at ``x0 + c * col_width``. Each cell keeps its label and
    value inside ``col_width - 4`` points (else `ValueError`). Use ``("", "")`` for a spacer."""
    if row_height <= VALUE_DY + 5.0:
        raise ValueError("row_height too small: value row would merge with the next label row")
    items: list[BoxItem] = []
    for r, row in enumerate(rows):
        for c, (label, value) in enumerate(row):
            if not label and not value:
                continue
            items += cell(
                x0 + c * col_width,
                y0 + r * row_height,
                label,
                value,
                width=col_width - 4.0,
                font_size=font_size,
            )
    return items


# --------------------------------------------------------------------------------------------
# Goldens
# --------------------------------------------------------------------------------------------


def golden_dict(doc: ParsedTaxDocument) -> dict[str, Any]:
    """`ParsedTaxDocument` -> a plain JSON-able dict (tuples become lists, key order stable under
    ``json.dumps(sort_keys=True)``). This is the representation goldens and parser tests share."""
    result: dict[str, Any] = json.loads(json.dumps(asdict(doc), sort_keys=True))
    if not result.get("unparsed"):
        result.pop("unparsed", None)  # absent in goldens that predate partial-success reporting
    for region in result.get("unparsed", []):
        region.pop("boxes", None)  # geometry for the LLM fallback's crop, not a parse result
    # Provenance defaults (all deterministic output) are absent in goldens that predate the
    # LLM fallback; a non-default value (``llm``) stays visible.
    for form in result.get("forms", []):
        if form.get("extraction_method") == "parser":
            form.pop("extraction_method")
        for item in (*form.get("income", []), *form.get("taxes", [])):
            if item.get("origin") == "parser":
                item.pop("origin")
    return result


def build_golden(
    doc: ParsedTaxDocument, *, expected_warnings: Sequence[str] = ()
) -> dict[str, Any]:
    """`golden_dict(doc)` plus an ``expected_warnings`` list (every document- and form-level
    warning the parser should emit, sorted). `tests.ingest.tax_support.assert_matches_golden`
    compares both parts."""
    golden = golden_dict(doc)
    golden["expected_warnings"] = sorted(expected_warnings)
    return golden
