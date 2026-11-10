"""Split a tax document into regions and segments by form anchors and word geometry.

Tax PDFs rarely hold "one form per file". A page can carry two forms side by side (2-up, or a 2x2
grid of 4-up W-2 copies), forms stacked, a form that continues over several pages, or different
form types in one package. Plain text extraction interleaves side-by-side forms on one line, so
this module works on word coordinates instead:

1. `find_anchors` locates form *headings* (``Form 1099-DIV``, the W-2 "Employee's social security
   number" box, ``Form 1040``) as the start of a text run, never inside prose.
2. `layout_regions` cuts each page into regions in reading order. When several anchors sit on one
   visual row far apart horizontally the page is cut into columns at a vertical gap that no word
   crosses (the cut is placed just left of the right-hand anchor, so a wide left form keeps its
   amounts); the band ends at the first row that crosses the cut. Where the family of form changes
   within a column the region is cut again.
3. `build_segments` groups consecutive regions of the same family into `Segment`s (a region with
   no anchor is a continuation of the previous segment, or the preamble of the next one on its
   own page). Each segment carries an `ExtractedDoc` of its regions, so every existing parser runs
   on it unchanged; a column region becomes its own `PageText`, so rows of the two forms can never
   mix.

A document in which nothing was cut yields its original pages (same objects): the single-form
path is identical to parsing the document directly.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from personal_finance_analyzer.core.types import ExtractedDoc, PageText, Word
from personal_finance_analyzer.ingest.layout import Row, cluster_rows, parse_money

#: Words further apart than this (points) start a new text run (cell) on a row.
_RUN_GAP = 14.0
#: Two anchors on one row are side by side when their left edges are this far apart.
_MIN_COLUMN_SEPARATION = 100.0
#: Longest heading run (in words) that can be an anchor; longer runs are running text.
_MAX_ANCHOR_WORDS = 14
#: How many rows below the anchor row are used to find the gap between two columns.
_SAMPLE_ROWS = 8
#: Narrowest free vertical stripe accepted as a column gap.
_MIN_GAP = 4.0
#: Preceding rows closer than this to an anchor row stay with that anchor's form (points).
_PREAMBLE_PITCH = 14.0

_A1099 = re.compile(r"^(?:20\d{2}\s+)?(?:Form\s*)?1099\s*-\s*([A-Z]{1,4})\b")
_A1040 = re.compile(r"^(?:Form\s*1040\b|U\.S\.\s+Individual\s+Income\s+Tax\s+Return\b)", re.I)
_W2_SSN = re.compile(r"Employee'?s\s+social\s+security\s+number", re.I)
_W2_WAGES = re.compile(r"Wages,?\s+tips,?\s+other\s+comp", re.I)
_CONTINUED = re.compile(
    r"\bcont(?:inued|'d)\b|\bpage\s+(?:[2-9]|\d{2,})\s+of\s+\d+\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class Anchor:
    """A form heading: its family (``"w2"``, ``"1099-INT"``, ``"1040"``), page and position."""

    family: str
    page: int
    row: int
    x0: float
    x1: float
    top: float


@dataclass(frozen=True, slots=True)
class Region:
    """A rectangular part of one original page, in reading order."""

    page: int
    rows: tuple[Row, ...]
    anchors: tuple[Anchor, ...]
    cut: bool  # False when the region is a whole, uncut page

    @property
    def family(self) -> str | None:
        return self.anchors[0].family if self.anchors else None


@dataclass(frozen=True, slots=True)
class Segment:
    """Consecutive regions of one family, as a parseable document.

    `page_map` maps each page number of `doc` back to the original page (identity when nothing
    was cut). `anchors` are the headings found inside it.
    """

    family: str | None
    doc: ExtractedDoc
    page_map: dict[int, int]
    anchors: tuple[Anchor, ...]

    @property
    def original_pages(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.page_map.values())))


# --------------------------------------------------------------------------------------------
# Anchors
# --------------------------------------------------------------------------------------------


def _runs(row: Row) -> list[list[Word]]:
    runs: list[list[Word]] = []
    for word in row.words:
        if runs and word.x0 - runs[-1][-1].x1 <= _RUN_GAP:
            runs[-1].append(word)
        else:
            runs.append([word])
    return runs


def find_anchors(rows: Sequence[Row], page: int) -> list[Anchor]:
    """Form headings on one page, in reading order (row, then x). A heading is a text run that
    starts with the form name; the W-2 anchor is the "Employee's social security number" label
    (or, on a page without one, the box 1 wages label). A run that says "continued" or
    "page 2 of 3" is a repeated running heading, not the start of a form."""
    anchors: list[Anchor] = []
    w2_ssn: list[Anchor] = []
    w2_wages: list[Anchor] = []
    for index, row in enumerate(rows):
        for run in _runs(row):
            text = " ".join(w.text for w in run)
            x0, x1 = run[0].x0, run[-1].x1
            if _CONTINUED.search(text):
                continue
            if len(run) <= _MAX_ANCHOR_WORDS:
                m = _A1099.match(text)
                if m is not None:
                    anchors.append(Anchor(f"1099-{m.group(1)}", page, index, x0, x1, row.top))
                    continue
                if _A1040.match(text):
                    anchors.append(Anchor("1040", page, index, x0, x1, row.top))
                    continue
            if _W2_SSN.search(text):
                w2_ssn.append(Anchor("w2", page, index, x0, x1, row.top))
            elif _W2_WAGES.search(text):
                w2_wages.append(Anchor("w2", page, index, x0, x1, row.top))
    anchors += w2_ssn or w2_wages
    anchors.sort(key=lambda a: (a.row, a.x0))
    return anchors


# --------------------------------------------------------------------------------------------
# Column cuts
# --------------------------------------------------------------------------------------------


def _free_intervals(words: Sequence[Word], lo: float, hi: float) -> list[tuple[float, float]]:
    """Sub-intervals of ``[lo, hi]`` that no word's interior covers."""
    spans = sorted((w.x0, w.x1) for w in words if w.x1 > lo and w.x0 < hi)
    free: list[tuple[float, float]] = []
    cursor = lo
    for start, end in spans:
        if start > cursor:
            free.append((cursor, min(start, hi)))
        cursor = max(cursor, end)
        if cursor >= hi:
            break
    if cursor < hi:
        free.append((cursor, hi))
    return [(a, b) for a, b in free if b > a]


def _column_cut(
    sample: Sequence[Word], left: Anchor, right: Anchor, lower: float, upper: float
) -> float | None:
    """x position of the gap between the form at `left` and the one at `right`, or `None`.

    Every free vertical stripe between the two headings is a candidate. The stripe that splits
    the words between `lower` and `upper` most evenly wins (the gap between two forms balances
    their mass; a stripe inside one form does not); a stripe right in front of the right-hand
    heading, where brokers align headings with their column, counts one and a half times."""
    words = [w for w in sample if lower <= (w.x0 + w.x1) / 2 < upper]
    best: tuple[float, float] | None = None
    for a, b in _free_intervals(sample, left.x0 + 10.0, right.x0 + 1.0):
        if b - a < _MIN_GAP:
            continue
        cut = (a + b) / 2
        left_n = sum(1 for w in words if (w.x0 + w.x1) / 2 < cut)
        right_n = len(words) - left_n
        score = float(min(left_n, right_n))
        if cut >= right.x0 - 14.0:
            score *= 1.5
        if best is None or score >= best[0]:
            best = (score, cut)
    return None if best is None or best[0] <= 0 else best[1]


def _straddles(row: Row, cuts: Sequence[float]) -> bool:
    return any(w.x0 < c - 0.5 and w.x1 > c + 0.5 for w in row.words for c in cuts)


def _column_rows(rows: Sequence[Row], cuts: Sequence[float]) -> list[list[Row]]:
    columns: list[list[list[Word]]] = [[] for _ in range(len(cuts) + 1)]
    bounds = [float("-inf"), *cuts, float("inf")]
    for row in rows:
        for index in range(len(columns)):
            words = [w for w in row.words if bounds[index] <= (w.x0 + w.x1) / 2 < bounds[index + 1]]
            if words:
                columns[index].append(words)
    return [[_row_of(words) for words in column] for column in columns]


def _row_of(words: Sequence[Word]) -> Row:
    return Row(
        words=tuple(words),
        top=min(w.top for w in words),
        bottom=max(w.bottom for w in words),
        text=" ".join(w.text for w in words),
    )


def _anchor_rows(anchors: Sequence[Anchor]) -> dict[int, list[Anchor]]:
    by_row: dict[int, list[Anchor]] = {}
    for anchor in anchors:
        by_row.setdefault(anchor.row, []).append(anchor)
    return by_row


def _row_cuts(rows: Sequence[Row], row_index: int, group: Sequence[Anchor]) -> list[float]:
    ordered = sorted(group, key=lambda a: a.x0)
    sample = [w for row in rows[row_index : row_index + _SAMPLE_ROWS + 1] for w in row.words]
    cuts: list[float] = []
    lower = float("-inf")
    for position, (left, right) in enumerate(pairwise(ordered), start=1):
        if right.x0 - left.x0 < _MIN_COLUMN_SEPARATION:
            continue
        upper = ordered[position + 1].x0 - 10.0 if position + 1 < len(ordered) else float("inf")
        cut = _column_cut(sample, left, right, lower, upper)
        if cut is not None:
            lower = cut
            cuts.append(cut)
    return sorted(cuts)


#: Rows needed to trust a column gap found from the pattern "amount ... label" (no heading anchors).
_MIN_GUTTER_ROWS = 3
#: Narrowest gap between an amount and the label of the next column.
_MIN_GUTTER = 12.0
_BOX_NUMBER = re.compile(r"^\d{1,2}[A-Za-z]?$")


def _money_like(text: str) -> bool:
    return (
        parse_money(text) is not None
        and any(c.isdigit() for c in text)
        and ("." in text or "," in text or "$" in text or text.startswith("("))
    )


def _gutter_candidates(row: Row) -> list[float]:
    """Cut positions this row suggests: an amount followed, after a wide gap, by a label."""
    found: list[float] = []
    words = row.words
    for i, (a, b) in enumerate(pairwise(words)):
        if not _money_like(a.text) or b.x0 - a.x1 < _MIN_GUTTER:
            continue
        nxt = words[i + 2].text if i + 2 < len(words) else ""
        starts_label = any(ch.isalpha() for ch in b.text) or (
            _BOX_NUMBER.match(b.text) is not None and bool(nxt[:1].isalpha())
        )
        if starts_label and not _money_like(b.text):
            found.append((a.x1 + b.x0) / 2)
    return found


def _split_money_columns(rows: list[Row]) -> list[tuple[list[Row], bool]]:
    """Split a run of rows into (rows, is_column) pieces where two tables sit side by side
    without headings: rows read "label amount  label amount" with the same gap throughout."""
    if len(rows) < 6:
        return [(rows, False)]
    votes: list[tuple[int, float]] = []
    for index, row in enumerate(rows):
        votes.extend((index, x) for x in _gutter_candidates(row))
    best: list[int] = []
    cut = 0.0
    for _, anchor_x in votes:
        support = sorted({i for i, x in votes if abs(x - anchor_x) <= 6.0})
        if len(support) > len(best):
            best = support
            cut = statistics.median(x for _, x in votes if abs(x - anchor_x) <= 6.0)
    if len(best) < _MIN_GUTTER_ROWS:
        return [(rows, False)]
    clean = [not _straddles(r, [cut]) for r in rows]
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for i, ok in enumerate([*clean, False]):
        if ok and start is None:
            start = i
        elif not ok and start is not None:
            runs.append((start, i))
            start = None
    support_set = set(best)
    lo, hi = max(runs, key=lambda r: sum(1 for i in range(*r) if i in support_set))
    if sum(1 for i in range(lo, hi) if i in support_set) < _MIN_GUTTER_ROWS:
        return [(rows, False)]
    pieces: list[tuple[list[Row], bool]] = []
    if lo > 0:
        pieces.extend(_split_money_columns(rows[:lo]))
    pieces.extend((column, True) for column in _column_rows(rows[lo:hi], [cut]) if column)
    if hi < len(rows):
        pieces.extend(_split_money_columns(rows[hi:]))
    return pieces


def _split_by_family(rows: Sequence[Row], anchors: Sequence[Anchor]) -> list[tuple[int, int]]:
    """``(start, end)`` row ranges of a single-column run of rows, cut where the form family
    changes (a W-2 stacked on a 1099). Rows just above the new heading (its top-of-form lines)
    go with it. `anchors` were found on `rows` themselves (`Anchor.row` indexes `rows`)."""
    starts = [0]
    previous: str | None = None
    for anchor in sorted(anchors, key=lambda a: (a.row, a.x0)):
        if previous is not None and anchor.family != previous:
            index = anchor.row
            while (
                index > starts[-1] + 1 and rows[index].top - rows[index - 1].top <= _PREAMBLE_PITCH
            ):
                index -= 1
            if index > starts[-1]:
                starts.append(index)
        previous = anchor.family
    bounds = [*starts, len(rows)]
    return [(a, b) for a, b in pairwise(bounds) if b > a]


def layout_regions(doc: ExtractedDoc) -> list[Region]:
    """Cut every page of `doc` into regions in reading order (see the module docstring)."""
    regions: list[Region] = []
    for page in doc.pages:
        rows = cluster_rows(page.words)
        if not rows:
            regions.append(Region(page.page_number, (), (), cut=False))
            continue
        anchors = find_anchors(rows, page.page_number)
        by_row = _anchor_rows(anchors)
        multi = sorted(i for i, group in by_row.items() if len(group) >= 2)
        pieces: list[tuple[list[Row], bool]] = []
        cursor = 0
        for position, row_index in enumerate(multi):
            if row_index < cursor:
                continue
            cuts = _row_cuts(rows, row_index, by_row[row_index])
            if not cuts:
                continue
            limit = multi[position + 1] if position + 1 < len(multi) else len(rows)
            end = row_index + 1
            while end < limit and not _straddles(rows[end], cuts):
                end += 1
            if row_index > cursor:
                pieces.append((list(rows[cursor:row_index]), False))
            for column in _column_rows(rows[row_index:end], cuts):
                if column:
                    pieces.append((column, True))
            cursor = end
        if cursor < len(rows):
            pieces.append((list(rows[cursor:]), False))
        pieces = [
            sub
            for rows_, is_col in pieces
            for sub in ([(rows_, True)] if is_col else _split_money_columns(rows_))
        ]
        for piece_rows, from_column in pieces:
            piece_anchors = find_anchors(piece_rows, page.page_number)
            spans = _split_by_family(piece_rows, piece_anchors)
            cut = from_column or len(pieces) > 1 or len(spans) > 1
            for start, end in spans:
                inside = tuple(a for a in piece_anchors if start <= a.row < end)
                regions.append(Region(page.page_number, tuple(piece_rows[start:end]), inside, cut))
    return regions


# --------------------------------------------------------------------------------------------
# Segments
# --------------------------------------------------------------------------------------------


def _page_text(page_number: int, rows: Sequence[Row]) -> PageText:
    text = "\n".join(r.text for r in rows)
    return PageText(
        page_number=page_number,
        text=text,
        words=tuple(w for r in rows for w in r.words),
        char_count=len(text),
    )


def _group_regions(regions: Sequence[Region]) -> list[tuple[str | None, list[Region]]]:
    groups: list[tuple[str | None, list[Region]]] = []
    pending: list[Region] = []
    for region in regions:
        family = region.family
        if family is None:
            pending.append(region)
            continue
        if groups and groups[-1][0] == family:
            groups[-1][1].extend(pending)
            groups[-1][1].append(region)
        else:
            lead = [r for r in pending if r.page == region.page]
            rest = [r for r in pending if r.page != region.page]
            if groups:
                groups[-1][1].extend(rest)
            else:
                lead = rest + lead
            groups.append((family, [*lead, region]))
        pending = []
    if pending:
        if groups:
            groups[-1][1].extend(pending)
        else:
            groups.append((None, pending))
    return groups


def build_segments(doc: ExtractedDoc) -> list[Segment]:
    """Group the regions of `doc` into segments (see the module docstring). Pages without any
    word still appear inside the segment they follow. An empty document yields no segments."""
    regions = layout_regions(doc)
    if not regions:
        return []
    any_cut = any(r.cut for r in regions)
    groups = _group_regions(regions)
    by_number = {p.page_number: p for p in doc.pages}
    segments: list[Segment] = []
    counter = 0
    for family, members in groups:
        pages: list[PageText] = []
        page_map: dict[int, int] = {}
        for region in members:
            if not any_cut:
                page = by_number[region.page]
                pages.append(page)
                page_map[page.page_number] = page.page_number
                continue
            counter += 1
            pages.append(_page_text(counter, region.rows))
            page_map[counter] = region.page
        anchors = tuple(a for r in members for a in r.anchors)
        segments.append(
            Segment(
                family,
                ExtractedDoc(
                    file_sha256=doc.file_sha256,
                    page_count=len(pages),
                    pages=tuple(pages),
                    has_text_layer=doc.has_text_layer,
                ),
                page_map,
                anchors,
            )
        )
    return segments
