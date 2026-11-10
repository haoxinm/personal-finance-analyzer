"""Fixture-generator framework (P0-7, frozen with §3).

`LayoutBuilder` is the protocol every layout's fixture generator implements (P1-B and P1-B1…B4).
This module also provides the shared `reportlab` rendering helpers so every builder produces PDFs
with the same, predictable text-layout properties that `personal_finance_analyzer.ingest.layout` expects:
one visual row per logical line, and column alignment via fixed-width padding in a monospaced font
so that word x-coordinates are deterministic and reproducible across runs.

Builders live in sibling modules of this package (e.g. ``tests/fixtures/gen/generic.py``,
``tests/fixtures/gen/layout_a.py``); `tests/generate_fixtures.py` discovers them by scanning this
package — there is no shared registry file for the parser work packages to conflict over.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

#: Default monospaced font. Deterministic, fixed-width character advance, so fixed-width padded
#: columns land at the same x-coordinate on every render — required for reproducible goldens.
DEFAULT_FONT = "Courier"
DEFAULT_FONT_SIZE = 9.0
DEFAULT_LINE_HEIGHT = 13.0
DEFAULT_LEFT_MARGIN = 50.0
DEFAULT_TOP_MARGIN = 56.0


class LayoutBuilder(Protocol):
    """A synthetic-fixture generator for one statement layout."""

    #: Stable id, matching the parser's `StatementParser.id` (e.g. ``'layout_a_credit'``).
    layout_id: str

    #: Variant names this builder supports, e.g. ``('normal', 'multiline', 'fx', 'refund', 'malformed')``.
    variants: tuple[str, ...]

    def build(self, out_dir: Path, *, variant: str, seed: int) -> dict[str, Any]:
        """Render a synthetic PDF to ``out_dir/<layout_id>_<variant>.pdf`` with `reportlab`.

        Args:
            out_dir: directory to write the PDF into (created if absent).
            variant: one of `variants`.
            seed: for deterministic pseudo-random content; the same ``(variant, seed)`` must
                render byte-for-byte identical transaction content across runs (not necessarily
                byte-identical PDF bytes, since reportlab embeds no run-to-run-varying metadata
                by default, but the *golden* below must be stable).

        Returns:
            The expected `ParsedStatement` as a JSON-able dict (the golden) — the fields a test
            will assert the real parser reproduces.
        """
        ...


def render_lines_pdf(
    path: Path,
    pages: Sequence[Sequence[str]],
    *,
    font_name: str = DEFAULT_FONT,
    font_size: float = DEFAULT_FONT_SIZE,
    line_height: float = DEFAULT_LINE_HEIGHT,
    left_margin: float = DEFAULT_LEFT_MARGIN,
    top_margin: float = DEFAULT_TOP_MARGIN,
    page_size: tuple[float, float] = letter,
) -> None:
    """Render ``pages`` (each a sequence of full text lines) to a PDF at ``path``, one physical
    line per logical line. `pdfplumber` recovers exactly one visual row per line via
    `personal_finance_analyzer.ingest.layout.cluster_rows`, and word x-coordinates follow directly from
    `font_name`'s (monospaced) character advance, so `format_row`'s fixed-width padding produces
    predictable column positions.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # `invariant=1` pins the document ID and creation/modification timestamps to fixed,
    # content-derived values instead of the wall clock and a random UUID, so two renders of the
    # same `pages` are byte-for-byte identical. Without it, every regenerate rewrites the PDF
    # even when its visible content is unchanged, which defeats reproducible, committed fixtures.
    pdf = canvas.Canvas(str(path), pagesize=page_size, invariant=1)
    _, page_height = page_size
    for page_lines in pages:
        pdf.setFont(font_name, font_size)
        y = page_height - top_margin
        for line in page_lines:
            if line:
                pdf.drawString(left_margin, y, line)
            y -= line_height
        pdf.showPage()
    pdf.save()


def format_row(cells: Sequence[str], widths: Sequence[int], *, gap: int = 2) -> str:
    """Left-justify each of ``cells`` to the matching width in ``widths`` and join with ``gap``
    spaces, trimming trailing whitespace. With a monospaced font, this makes every column start
    at a fixed, predictable x-coordinate across every row that uses the same `widths`."""
    if len(cells) != len(widths):
        raise ValueError(f"cells and widths must have equal length: {len(cells)} != {len(widths)}")
    parts = [cell.ljust(width) for cell, width in zip(cells, widths, strict=True)]
    return (" " * gap).join(parts).rstrip()
