"""Shared layout primitives (A30, P0-7).

Every parser, the generic fallback, the layout-spec interpreter, and issuer matching need the same
geometry and money handling. Written once here and frozen with §3 — no Phase 1 lane re-implements
row clustering, column bands, table location, money parsing, date parsing, year inference, or
statement-summary extraction.

Money is parsed **only** by `parse_money` (never ad hoc elsewhere), using `Decimal`, never
`float` — a `float` here is where sign and rounding bugs live (I5 is the single most common
source of bugs in this codebase).
"""

from __future__ import annotations

import itertools
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.types import ExtractedDoc, Word

# --------------------------------------------------------------------------------------------
# Geometry types (module-local: not part of the frozen §3.1 core types, which are the parser
# I/O boundary; these are intermediate results used while building a ParsedStatement).
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Row:
    """One visual row of a page: words clustered by y-coordinate, sorted left to right."""

    words: tuple[Word, ...]
    top: float
    bottom: float
    text: str  # words joined by a single space, in x-order


@dataclass(frozen=True, slots=True)
class Band:
    """A column's horizontal extent, in page x-coordinates. ``x1`` is exclusive."""

    name: str
    x0: float
    x1: float


#: A row's role within a located table band.
RowKind = str  # "header" | "section" | "data" | "continuation" | "total"


@dataclass(frozen=True, slots=True)
class TableBand:
    """A located table region on one page (§2e.2 Stage 1)."""

    page_number: int
    header_row_index: int | None  # index into the page's `cluster_rows(...)` output, or None
    row_indices: tuple[int, ...]  # indices into the page's `cluster_rows(...)` output
    rows: tuple[Row, ...]  # same length/order as row_indices
    row_kinds: tuple[RowKind, ...]  # aligned with `rows`


@dataclass(frozen=True, slots=True)
class ParsedMoney:
    """The result of parsing a money-shaped token.

    ``magnitude_minor`` is always non-negative; ``printed_negative`` records whether the token
    was printed as a credit/negative (leading ``-``, parens, trailing ``-``, or a trailing
    ``CR``). Mapping this to the signed `RawTransaction.amount_minor` (I5) is the parser's job,
    not this function's — the same printed sign can mean different things in different sections.
    """

    magnitude_minor: int
    printed_negative: bool


@dataclass(frozen=True, slots=True)
class PartialDate:
    """A date as printed, which may or may not carry an explicit year."""

    month: int
    day: int
    year: int | None


@dataclass(frozen=True, slots=True)
class StatementSummary:
    """Statement-level facts read from the summary block. Any field is `None` when the summary
    block could not be located or that particular fact could not be read from it."""

    period_start: date | None
    period_end: date | None
    opening_balance_minor: int | None  # A25, sign as printed
    closing_balance_minor: int | None  # A25, sign as printed


# --------------------------------------------------------------------------------------------
# Row clustering and column bands
# --------------------------------------------------------------------------------------------


def cluster_rows(words: Sequence[Word], *, tol: float | None = None) -> tuple[Row, ...]:
    """Cluster ``words`` into visual rows by y-coordinate.

    Args:
        words: words from one page, in any order.
        tol: maximum difference in `top` from a row's anchor word for a word to join that row.
            Defaults to half the median glyph height (``bottom - top``) of ``words``.

    Returns:
        Rows in top-to-bottom order; words within a row are sorted left to right.
    """
    if not words:
        return ()

    if tol is None:
        heights = [w.bottom - w.top for w in words if w.bottom > w.top]
        median_height = statistics.median(heights) if heights else 10.0
        tol = median_height / 2

    ordered = sorted(words, key=lambda w: w.top)
    groups: list[list[Word]] = []
    anchor: float | None = None
    for word in ordered:
        if anchor is not None and abs(word.top - anchor) <= tol:
            groups[-1].append(word)
        else:
            groups.append([word])
            anchor = word.top

    rows: list[Row] = []
    for group in groups:
        group_sorted = sorted(group, key=lambda w: w.x0)
        rows.append(
            Row(
                words=tuple(group_sorted),
                top=min(w.top for w in group_sorted),
                bottom=max(w.bottom for w in group_sorted),
                text=" ".join(w.text for w in group_sorted),
            )
        )
    rows.sort(key=lambda r: r.top)
    return tuple(rows)


def infer_column_bands(rows: Sequence[Row], *, min_gap: float = 8.0) -> tuple[Band, ...]:
    """Infer column x-bands from recurring within-row whitespace gaps.

    A gap between two adjacent words on a row is a *candidate* column boundary only if it recurs,
    at roughly the same x-position, across a meaningful fraction of ``rows`` — an isolated gap is
    just the space between two words of a running description, not a column edge.

    Args:
        rows: rows to infer bands from (typically one table band's rows).
        min_gap: minimum horizontal gap (in points) between two words for it to be considered a
            candidate column boundary, and the clustering tolerance for merging nearby
            candidates.

    Returns:
        Bands left to right, covering the full x-extent of ``rows``. Empty if ``rows`` has no
        words.
    """
    all_words = [w for row in rows for w in row.words]
    if not all_words:
        return ()

    min_x = min(w.x0 for w in all_words)
    max_x = max(w.x1 for w in all_words)

    candidates: list[float] = []
    for row in rows:
        ordered = sorted(row.words, key=lambda w: w.x0)
        for a, b in itertools.pairwise(ordered):
            gap = b.x0 - a.x1
            if gap >= min_gap:
                candidates.append((a.x1 + b.x0) / 2)

    if not candidates:
        return (Band(name="col_0", x0=min_x, x1=max_x + 1.0),)

    candidates.sort()
    clusters: list[list[float]] = [[candidates[0]]]
    for x in candidates[1:]:
        if x - clusters[-1][-1] <= min_gap:
            clusters[-1].append(x)
        else:
            clusters.append([x])

    threshold = max(1, round(len(rows) * 0.3))
    boundaries = sorted(
        statistics.mean(cluster) for cluster in clusters if len(cluster) >= threshold
    )

    starts = [min_x, *boundaries]
    bands: list[Band] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else max_x + 1.0
        bands.append(Band(name=f"col_{i}", x0=start, x1=end))
    return tuple(bands)


def assign_to_bands(row: Row, bands: Sequence[Band]) -> dict[str, str]:
    """Assign each word of ``row`` to the band its `x0` falls within, and join each band's words
    into one string, left to right. A band that gets no words maps to ``""``."""
    buckets: dict[str, list[str]] = {band.name: [] for band in bands}
    for word in row.words:
        band = _band_for_x(word.x0, bands)
        if band is not None:
            buckets[band.name].append(word.text)
    return {name: " ".join(words) for name, words in buckets.items()}


def _band_for_x(x: float, bands: Sequence[Band]) -> Band | None:
    for band in bands:
        if band.x0 <= x < band.x1:
            return band
    if not bands:
        return None
    return min(bands, key=lambda b: min(abs(x - b.x0), abs(x - b.x1)))


# --------------------------------------------------------------------------------------------
# Table location (§2e.2 Stage 1)
# --------------------------------------------------------------------------------------------

_DATE_TOKEN_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b")
_MONTH_NAME_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-zA-Z]*\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)
_TOTAL_ROW_RE = re.compile(r"\btotal\b", re.IGNORECASE)

_DATE_TRY_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%m/%d", "%m-%d-%Y", "%m-%d")


def _row_left_right_text(row: Row) -> tuple[str, str]:
    """Split a row's words into its left third and right third by x-position, for the
    date-left / money-right heuristic."""
    if not row.words:
        return "", ""
    x0 = min(w.x0 for w in row.words)
    x1 = max(w.x1 for w in row.words)
    span = x1 - x0
    if span <= 0:
        text = row.text
        return text, text
    left_cutoff = x0 + span / 3
    right_cutoff = x1 - span / 3
    left_words = [w.text for w in row.words if w.x0 <= left_cutoff]
    right_words = [w.text for w in row.words if w.x1 >= right_cutoff]
    return " ".join(left_words), " ".join(right_words)


def _has_date_like(text: str) -> bool:
    if parse_date(text.strip(), _DATE_TRY_FORMATS) is not None:
        return True
    return bool(_DATE_TOKEN_RE.search(text) or _MONTH_NAME_RE.search(text))


def _has_money_like(text: str) -> bool:
    for token in text.split():
        if parse_money(token) is not None:
            return True
    return parse_money(text) is not None


def _is_row_bearing(row: Row) -> bool:
    left, right = _row_left_right_text(row)
    return _has_date_like(left) and _has_money_like(right)


def _find_runs(
    flags: Sequence[bool], *, min_run: int = 3, max_gap: int = 2, min_density: float = 0.6
) -> list[tuple[int, int]]:
    """Maximal runs of `True` values, tolerating gaps of up to `max_gap` consecutive `False`
    values, kept only when the run's length is >= `min_run` and its `True` density is
    >= `min_density`."""
    n = len(flags)
    runs: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if not flags[i]:
            i += 1
            continue
        start = i
        end = i
        gap = 0
        j = i + 1
        while j < n:
            if flags[j]:
                end = j
                gap = 0
            else:
                gap += 1
                if gap > max_gap:
                    break
            j += 1
        length = end - start + 1
        density = sum(flags[start : end + 1]) / length
        if length >= min_run and density >= min_density:
            runs.append((start, end))
        i = end + 1
    return runs


def _find_header_above(rows: Sequence[Row], start: int, *, look_back: int = 3) -> int | None:
    for idx in range(start - 1, max(start - look_back - 1, -1), -1):
        row = rows[idx]
        if not row.text.strip():
            continue
        _, right = _row_left_right_text(row)
        if not _has_money_like(right):
            return idx
    return None


def _classify_rows(rows: Sequence[Row], start: int, end: int) -> tuple[RowKind, ...]:
    kinds: list[RowKind] = []
    for idx in range(start, end + 1):
        row = rows[idx]
        text = row.text.strip()
        _, right = _row_left_right_text(row)
        money_right = _has_money_like(right)
        if _TOTAL_ROW_RE.search(text):
            kinds.append("total")
        elif not text:
            kinds.append("continuation")
        elif not money_right:
            kinds.append(
                "section"
                if text == text.upper() and any(c.isalpha() for c in text)
                else "continuation"
            )
        else:
            kinds.append("data")
    return tuple(kinds)


def _extend_trailing_continuation(
    rows: Sequence[Row], flags: Sequence[bool], start: int, end: int, *, limit: int
) -> int:
    """Extend a located run's ``end`` into trailing rows that are themselves non-date-bearing but
    still belong to the table.

    `_find_runs` tolerates gaps of non-bearing rows *between* two bearing rows, but a trailing run
    of non-bearing rows that never resumes before the page (or the next table band) ends is left
    out of the run entirely — this is what drops a multi-line description continuation or an FX
    info line when it happens to be the last line of a table on a page.

    A trailing row is included when it plausibly continues the table: non-blank, not a totals/
    summary line, not an all-caps section heading, and — the key guard against swallowing a page
    footer — indented past the table's leftmost (date) column, the way a wrapped description or
    FX line is, rather than starting flush with the date column the way page furniture typically
    does. Extension stops at the first row that fails any of these.
    """
    data_rows = [rows[i] for i in range(start, end + 1) if flags[i]]
    if not data_rows:
        return end
    bands = infer_column_bands(data_rows)
    if not bands:
        return end
    date_band_end = bands[0].x1

    new_end = end
    idx = end + 1
    while idx < limit and idx < len(rows):
        row = rows[idx]
        text = row.text.strip()
        if not text or not row.words:
            break  # blank row: a clear gap
        if _TOTAL_ROW_RE.search(text):
            break  # a totals/summary line, not part of the table
        if text == text.upper() and any(c.isalpha() for c in text):
            break  # an all-caps section heading (or footer), not a continuation
        row_x0 = min(w.x0 for w in row.words)
        if row_x0 < date_band_end:
            break  # starts flush with the date column: page furniture, not an indented wrap
        new_end = idx
        idx += 1
    return new_end


def locate_table_bands(doc: ExtractedDoc) -> tuple[TableBand, ...]:
    """Locate transaction table region(s) in ``doc`` (§2e.2 Stage 1, deterministic and local).

    A table band is a maximal run of >=3 consecutive rows where >=60% carry a date-like token in
    their left third and a money-like token in their right third, tolerating gaps of <=2 rows
    (multi-line description continuations). The run is then extended into any trailing
    non-date-bearing rows that still belong to the table — a wrapped description or FX line that
    is the last line of the table on its page (see `_extend_trailing_continuation`) — without
    swallowing a page footer or summary block. The nearest non-numeric row above is treated as the
    column-header row. Returns one `TableBand` per located region, across all pages; an empty
    tuple when no table is found anywhere in the document.
    """
    bands: list[TableBand] = []
    for page in doc.pages:
        rows = cluster_rows(page.words)
        if not rows:
            continue
        flags = [_is_row_bearing(row) for row in rows]
        runs = _find_runs(flags)
        for run_index, (start, end) in enumerate(runs):
            limit = runs[run_index + 1][0] if run_index + 1 < len(runs) else len(rows)
            end = _extend_trailing_continuation(rows, flags, start, end, limit=limit)
            header_idx = _find_header_above(rows, start)
            bands.append(
                TableBand(
                    page_number=page.page_number,
                    header_row_index=header_idx,
                    row_indices=tuple(range(start, end + 1)),
                    rows=rows[start : end + 1],
                    row_kinds=_classify_rows(rows, start, end),
                )
            )
    return tuple(bands)


# --------------------------------------------------------------------------------------------
# Money and date parsing
# --------------------------------------------------------------------------------------------

_MONEY_RE = re.compile(
    r"""^\s*
    (?P<paren_open>\()?
    \s*(?P<sign>-)?
    \s*\$?\s*
    (?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)
    \s*(?P<paren_close>\))?
    \s*(?P<trailing_sign>-)?
    \s*(?P<cr>CR)?
    \s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def parse_money(text: str) -> ParsedMoney | None:
    """Parse a money-shaped token into its magnitude and printed sign.

    Understands ``1,234.56``, ``-12.34``, ``(12.34)``, ``12.34 CR``, ``12.34-``, a leading ``$``,
    and leading/trailing spaces around any of the above. Uses `Decimal` throughout, never
    `float`. Returns `None` for anything that does not match a recognized money shape (this is
    not an error — most tokens on a statement page are not amounts).
    """
    if not text:
        return None
    match = _MONEY_RE.match(text.strip())
    if match is None:
        return None

    paren_open = bool(match.group("paren_open"))
    paren_close = bool(match.group("paren_close"))
    if paren_open != paren_close:
        return None  # unbalanced parens: not a valid money shape

    amount_str = match.group("amount").replace(",", "")
    try:
        amount = Decimal(amount_str)
    except InvalidOperation:  # pragma: no cover - regex already constrains the shape
        return None

    negative = (
        bool(match.group("sign"))
        or paren_open
        or bool(match.group("trailing_sign"))
        or bool(match.group("cr"))
    )
    minor = int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))
    return ParsedMoney(magnitude_minor=minor, printed_negative=negative)


def parse_date(text: str, formats: Sequence[str]) -> PartialDate | None:
    """Try each of ``formats`` (`datetime.strptime` patterns) against ``text`` in order; return
    the first successful parse as a `PartialDate`. `year` is `None` when the matched format has
    no year directive (``%Y``/``%y``) — such a date carries no year as printed, and inferring one
    is `infer_year`'s job, not this function's."""
    cleaned = text.strip()
    if not cleaned:
        return None
    for fmt in formats:
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        has_year = "%Y" in fmt or "%y" in fmt
        return PartialDate(
            month=parsed.month, day=parsed.day, year=parsed.year if has_year else None
        )
    return None


def infer_year(partial: PartialDate, period_start: date, period_end: date) -> date:
    """Resolve a year-less `PartialDate` against a statement period.

    The year is chosen from ``{period_start.year, period_end.year}``, whichever places the
    resulting date inside ``[period_start - 5 days, period_end + 5 days]`` — this is how a
    December date on a January statement correctly lands in the prior year.

    Raises:
        ParserError: if `partial.year` is `None` and neither candidate year lands the date in
            that window (or if `partial` already carries a year, this never raises).
    """
    if partial.year is not None:
        return date(partial.year, partial.month, partial.day)

    window_start = period_start - timedelta(days=5)
    window_end = period_end + timedelta(days=5)
    for year in sorted({period_start.year, period_end.year}):
        try:
            candidate = date(year, partial.month, partial.day)
        except ValueError:
            continue
        if window_start <= candidate <= window_end:
            return candidate

    raise ParserError(
        f"could not infer year for date {partial.month:02d}/{partial.day:02d} within statement "
        f"period {period_start.isoformat()} - {period_end.isoformat()}"
    )


# --------------------------------------------------------------------------------------------
# Statement summary (period + balances)
# --------------------------------------------------------------------------------------------

_PERIOD_PATTERNS = (
    re.compile(
        r"(?:Opening|Statement|Billing)\s*(?:Closing)?\s*Date[:\s]*[-]?\s*"
        r"(\d{1,2}/\d{1,2}/\d{2,4}).{0,60}?"
        r"(?:Closing|Closing\s*Date)[:\s]*[-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"([A-Za-z]+ \d{1,2},? \d{4})\s*(?:through|to|-)\s*([A-Za-z]+ \d{1,2},? \d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s*(?:through|to|-)\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        re.IGNORECASE,
    ),
)

_BALANCE_PATTERNS = (
    (
        re.compile(r"Previous Balance[:\s]*\$?\s*([\d,]+\.\d{2})", re.IGNORECASE),
        re.compile(r"New Balance[:\s]*\$?\s*([\d,]+\.\d{2})", re.IGNORECASE),
    ),
    (
        re.compile(r"Beginning Balance[:\s]*\$?\s*(\(?-?[\d,]+\.\d{2}\)?)", re.IGNORECASE),
        re.compile(r"Ending Balance[:\s]*\$?\s*(\(?-?[\d,]+\.\d{2}\)?)", re.IGNORECASE),
    ),
)

_FLEXIBLE_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y")


def _parse_flexible_date(text: str) -> date | None:
    cleaned = text.strip()
    for fmt in _FLEXIBLE_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _signed_minor(parsed: ParsedMoney | None) -> int | None:
    if parsed is None:
        return None
    return -parsed.magnitude_minor if parsed.printed_negative else parsed.magnitude_minor


def extract_period_and_balances(doc: ExtractedDoc) -> StatementSummary:
    """Read the statement period and opening/closing balances from the summary block.

    Every field is `None` when it could not be located — this function never guesses or infers
    a year from today's date; a parser that needs the period and cannot get it here must raise
    `ParserError` itself (per §2c common requirements).
    """
    text = doc.full_text

    period_start: date | None = None
    period_end: date | None = None
    for pattern in _PERIOD_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        start = _parse_flexible_date(match.group(1))
        end = _parse_flexible_date(match.group(2))
        if start is not None and end is not None:
            period_start, period_end = start, end
            break

    opening_minor: int | None = None
    closing_minor: int | None = None
    for open_pattern, close_pattern in _BALANCE_PATTERNS:
        open_match = open_pattern.search(text)
        close_match = close_pattern.search(text)
        if open_match is not None and close_match is not None:
            opening_minor = _signed_minor(parse_money(open_match.group(1)))
            closing_minor = _signed_minor(parse_money(close_match.group(1)))
            break

    return StatementSummary(
        period_start=period_start,
        period_end=period_end,
        opening_balance_minor=opening_minor,
        closing_balance_minor=closing_minor,
    )
