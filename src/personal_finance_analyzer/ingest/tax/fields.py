"""Labelled-amount primitives for tax forms (plan 2.3, D7).

DEVELOPER NOTE (the API per-form parsers build on)
==================================================
Two layouts exist in real tax PDFs and both are supported by the same call:

* "same row": label and value on one visual row, value to the right
  (software-generated 1040, consolidated 1099):  ``Wages, salaries, tips ...  1a   85,000``
* "below": IRS box layout, small label line with the value on the row beneath it, inside the same
  x-range (W-2, single 1099s).

Everything works on `ExtractedDoc` word boxes (`PageText.words`), rows via `layout.cluster_rows`,
money via `layout.parse_money` (Decimal, never float). Row indices are per page.

Signatures (all keyword-only after the label; ``pages`` are 1-based page numbers)::

    compile_label(pattern: str) -> re.Pattern[str]                    # re.IGNORECASE
    find_label(doc, label_re, *, pages=None) -> list[LabelHit]
    money_for_label(doc, label_re, *, mode="auto", occurrence=0, nth=0, pages=None,
                    x_tol=12.0, max_rows_below=2, skip_line_numbers=True) -> ParsedMoney | None
    amount_for_label(doc, label_re, **same) -> int | None            # magnitude, minor units
    signed_amount_for_label(doc, label_re, **same) -> int | None     # negative if printed negative
    text_for_label(doc, label_re, *, mode="auto", occurrence=0, pages=None, lines=1,
                   x_tol=12.0, max_gap=8.0) -> str | None            # SCRUBBED (tin.scrub)
    whole_dollar_or_cents(token: str) -> int | None                  # "85,000" -> 8500000
    signed(money: ParsedMoney | None) -> int | None
    split_blocks(doc, start_re, *, end_re=None, keep_preamble=False) -> tuple[ExtractedDoc, ...]
    first_block(doc, start_re, end_re=None) -> ExtractedDoc          # alias: first_page_block
    doc_has(doc, pattern, *, pages=None) -> bool                      # regex anywhere in text
    checkbox_checked(doc, label_re, *, pages=None) -> bool
    corrected_marker(doc) -> bool                                     # "CORRECTED" checked/banner

Usage::

    box1 = amount_for_label(doc, compile_label(r"Wages, tips, other compensation"), mode="below")
    line1a = amount_for_label(doc, compile_label(r"Wages, salaries, tips"), mode="same_row")
    st_gain = money_for_label(doc, compile_label(r"short-term"), mode="same_row", nth=2)
    net = signed(st_gain)                       # D6: capital loss is negative
    first = first_block(doc, compile_label(r"Wage and Tax Statement"))   # one copy of a W-2
    forms = split_blocks(doc, compile_label(r"Form 1099-INT"))           # one doc per form

Rules of thumb:
* ``amount_for_label`` returns the *magnitude*; use ``money_for_label`` + ``signed`` (or
  ``signed_amount_for_label``) when the printed sign matters (D6).
* A label must sit on ONE row; it is matched against the row's space-joined text.
* ``same_row`` takes the first money-shaped word right of the label. A bare 1-3 digit integer is
  skipped when it looks like a box/line number (next word starts with a letter, or another money
  token follows on the row) unless ``skip_line_numbers=False``. ``nth`` picks the nth remaining
  money token on the row (column 0, 1, 2...), e.g. Proceeds / Cost / Gain tables.
* ``below`` scans up to ``max_rows_below`` rows underneath. A row with letters inside the label's
  x-range is a label row: if it starts with a box number (``3 Social security wages``) it is the
  next cell, so the search stops (a blank box never steals the value of the cell beneath);
  otherwise it is a wrapped continuation of the label and is skipped. Labels without box
  numbers: use ``max_rows_below=1`` when the label does not wrap. Candidates overlapping the label's own x-range beat those only within
  ``x_tol`` points of it.
* ``auto`` = same_row, then below. Prefer the explicit mode when you know the layout.
* ``text_for_label`` output is always passed through ``tin.scrub``; still pass payer names through
  ``common.sanitize_payer_name``.
* ``split_blocks`` starts a block on each row matching ``start_re`` and runs it to the next start
  row (across pages), or to a row matching ``end_re``. ``first_block`` returns the original doc
  unchanged when ``start_re`` never matches. Side-by-side forms on one row band are not split.

This is a helper module: it exposes no object with `id`/`version`/`form_types`/`detect`/`parse`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from personal_finance_analyzer.core.types import ExtractedDoc, PageText, Word
from personal_finance_analyzer.ingest.layout import ParsedMoney, Row, cluster_rows, parse_money
from personal_finance_analyzer.ingest.tax.tin import scrub

Mode = Literal["same_row", "below", "auto"]

#: Horizontal tolerance (points) for matching a value to its label's cell in "below" mode.
_X_TOL = 12.0
#: How many rows under the label "below" mode will look.
_MAX_ROWS_BELOW = 2
#: Maximum gap (points) between words that still belong to the same text run.
_MAX_TEXT_GAP = 8.0

_BARE_SMALL_INT = re.compile(r"^\d{1,3}$")
_BOX_NUMBER = re.compile(r"^\d{1,2}[A-Za-z]?$")
_STARTS_ALPHA = re.compile(r"^[A-Za-z]")
_HAS_LETTER = re.compile(r"[A-Za-z]")
_LEADER_ONLY = re.compile(r"^[.·:_\-…\s]+$")

_MARK = r"(?:\[\s*[xX✓✔]\s*\]|\(\s*[xX✓✔]\s*\)|[☒☑✓✔])"
_EMPTY_BOX = r"(?:\[\s*\]|\(\s*\)|☐)"
_CORRECTED_RE = re.compile(r"\bCORRECTED\b", re.IGNORECASE)


def compile_label(pattern: str) -> re.Pattern[str]:
    """Compile a label regex case-insensitively (the usual way to build a `label_re`)."""
    return re.compile(pattern, re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LabelHit:
    """Where a label matched: `row_index` indexes `cluster_rows(page.words)` of that page; the
    x-extent is that of the words the match overlaps."""

    page_number: int
    row_index: int
    label_x0: float
    label_x1: float
    top: float
    bottom: float


# --------------------------------------------------------------------------------------------
# Money helpers
# --------------------------------------------------------------------------------------------


def whole_dollar_or_cents(token: str) -> int | None:
    """Magnitude in minor units of a money token printed in whole dollars (``85,000``) or with
    cents (``85,000.50``); `None` if it is not money-shaped. Thin wrapper over `parse_money`
    (which accepts bare integers); the sign is dropped, use `parse_money` directly for it."""
    parsed = parse_money(token)
    return None if parsed is None else parsed.magnitude_minor


def signed(money: ParsedMoney | None) -> int | None:
    """Signed minor units: negative when the token was printed negative (``-``, parens, ``CR``)."""
    if money is None:
        return None
    return -money.magnitude_minor if money.printed_negative else money.magnitude_minor


# --------------------------------------------------------------------------------------------
# Rows and label search
# --------------------------------------------------------------------------------------------


def _page_rows(doc: ExtractedDoc, pages: Sequence[int] | None) -> dict[int, tuple[Row, ...]]:
    wanted = None if pages is None else set(pages)
    return {
        page.page_number: cluster_rows(page.words)
        for page in doc.pages
        if wanted is None or page.page_number in wanted
    }


def _hits_in(rows_by_page: dict[int, tuple[Row, ...]], label_re: re.Pattern[str]) -> list[LabelHit]:
    hits: list[LabelHit] = []
    for page_number, rows in rows_by_page.items():
        for row_index, row in enumerate(rows):
            spans: list[tuple[int, int]] = []
            pos = 0
            for word in row.words:
                spans.append((pos, pos + len(word.text)))
                pos += len(word.text) + 1
            for match in label_re.finditer(row.text):
                if match.end() <= match.start():
                    continue
                covered = [
                    w
                    for w, (s, e) in zip(row.words, spans, strict=True)
                    if s < match.end() and e > match.start()
                ]
                if not covered:
                    continue
                hits.append(
                    LabelHit(
                        page_number=page_number,
                        row_index=row_index,
                        label_x0=min(w.x0 for w in covered),
                        label_x1=max(w.x1 for w in covered),
                        top=row.top,
                        bottom=row.bottom,
                    )
                )
    return hits


def row_for_label(
    doc: ExtractedDoc,
    label_re: re.Pattern[str],
    *,
    occurrence: int = 0,
    pages: Sequence[int] | None = None,
) -> tuple[Row, LabelHit] | None:
    """The row holding the `occurrence`-th match of `label_re` and the match's `LabelHit`, or
    `None`. For callers that need their own same-row rule (`same_row_candidates`)."""
    rows_by_page = _page_rows(doc, pages)
    hits = _hits_in(rows_by_page, label_re)
    if occurrence < 0 or occurrence >= len(hits):
        return None
    hit = hits[occurrence]
    return rows_by_page[hit.page_number][hit.row_index], hit


def find_label(
    doc: ExtractedDoc, label_re: re.Pattern[str], *, pages: Sequence[int] | None = None
) -> list[LabelHit]:
    """Every place `label_re` matches within a single row, in reading order (page, row, x)."""
    return _hits_in(_page_rows(doc, pages), label_re)


# --------------------------------------------------------------------------------------------
# Same-row and below extraction
# --------------------------------------------------------------------------------------------


def _words_right_of(row: Row, hit: LabelHit) -> list[Word]:
    return [w for w in row.words if w.x0 >= hit.label_x1 - 0.5]


def same_row_candidates(row: Row, hit: LabelHit, *, skip_line_numbers: bool) -> list[ParsedMoney]:
    words = _words_right_of(row, hit)
    tokens: list[tuple[str, ParsedMoney, str | None]] = []
    for i, word in enumerate(words):
        money = parse_money(word.text)
        if money is None:
            continue
        nxt = words[i + 1].text if i + 1 < len(words) else None
        tokens.append((word.text, money, nxt))
    if not skip_line_numbers:
        return [m for _, m, _ in tokens]
    kept: list[ParsedMoney] = []
    for idx, (text, money, nxt) in enumerate(tokens):
        if _BARE_SMALL_INT.match(text):
            if nxt is not None and _STARTS_ALPHA.match(nxt):
                continue  # box number prefixing the next cell's label
            if idx + 1 < len(tokens):
                continue  # line number before the real amount
        kept.append(money)
    return kept


def _overlaps(word: Word, x0: float, x1: float) -> bool:
    return word.x1 >= x0 and word.x0 <= x1


def _below_candidates(
    rows: tuple[Row, ...],
    hit: LabelHit,
    *,
    x_tol: float,
    max_rows_below: int,
) -> list[ParsedMoney]:
    lo, hi = hit.label_x0 - x_tol, hit.label_x1 + x_tol
    for offset in range(1, max_rows_below + 1):
        index = hit.row_index + offset
        if index >= len(rows):
            return []
        in_range = [w for w in rows[index].words if _overlaps(w, lo, hi)]
        if not in_range:
            continue
        if any(parse_money(w.text) is None and _HAS_LETTER.search(w.text) for w in in_range):
            if _BOX_NUMBER.match(in_range[0].text):
                return []  # the next cell's label: this box is blank, do not steal its value
            continue  # a wrapped continuation of this label
        scored = [(w, parse_money(w.text)) for w in in_range]
        money = [(w, m) for w, m in scored if m is not None]
        if not money:
            continue
        strict = [(w, m) for w, m in money if min(w.x1, hit.label_x1) - max(w.x0, hit.label_x0) > 0]
        chosen = strict or money
        chosen.sort(key=lambda wm: wm[0].x0)
        return [m for _, m in chosen]
    return []


def money_for_label(
    doc: ExtractedDoc,
    label_re: re.Pattern[str],
    *,
    mode: Mode = "auto",
    occurrence: int = 0,
    nth: int = 0,
    pages: Sequence[int] | None = None,
    x_tol: float = _X_TOL,
    max_rows_below: int = _MAX_ROWS_BELOW,
    skip_line_numbers: bool = True,
) -> ParsedMoney | None:
    """The money value for the `occurrence`-th match of `label_re`, as `ParsedMoney` (magnitude
    plus printed sign), or `None` when the label or a value is not found. See module docstring
    for mode semantics. Never raises on missing data."""
    rows_by_page = _page_rows(doc, pages)
    hits = _hits_in(rows_by_page, label_re)
    if occurrence < 0 or occurrence >= len(hits) or nth < 0:
        return None
    hit = hits[occurrence]
    rows = rows_by_page[hit.page_number]
    candidates: list[ParsedMoney] = []
    if mode in ("same_row", "auto"):
        candidates = same_row_candidates(
            rows[hit.row_index], hit, skip_line_numbers=skip_line_numbers
        )
    if not candidates and mode in ("below", "auto"):
        candidates = _below_candidates(rows, hit, x_tol=x_tol, max_rows_below=max_rows_below)
    return candidates[nth] if nth < len(candidates) else None


def amount_for_label(
    doc: ExtractedDoc,
    label_re: re.Pattern[str],
    *,
    mode: Mode = "auto",
    occurrence: int = 0,
    nth: int = 0,
    pages: Sequence[int] | None = None,
    x_tol: float = _X_TOL,
    max_rows_below: int = _MAX_ROWS_BELOW,
    skip_line_numbers: bool = True,
) -> int | None:
    """Magnitude in minor units of the labelled amount (sign dropped, callers apply D6), or
    `None`. Same arguments as `money_for_label`."""
    money = money_for_label(
        doc,
        label_re,
        mode=mode,
        occurrence=occurrence,
        nth=nth,
        pages=pages,
        x_tol=x_tol,
        max_rows_below=max_rows_below,
        skip_line_numbers=skip_line_numbers,
    )
    return None if money is None else money.magnitude_minor


def signed_amount_for_label(
    doc: ExtractedDoc,
    label_re: re.Pattern[str],
    *,
    mode: Mode = "auto",
    occurrence: int = 0,
    nth: int = 0,
    pages: Sequence[int] | None = None,
    x_tol: float = _X_TOL,
    max_rows_below: int = _MAX_ROWS_BELOW,
    skip_line_numbers: bool = True,
) -> int | None:
    """Like `amount_for_label` but negative when the value was printed negative."""
    return signed(
        money_for_label(
            doc,
            label_re,
            mode=mode,
            occurrence=occurrence,
            nth=nth,
            pages=pages,
            x_tol=x_tol,
            max_rows_below=max_rows_below,
            skip_line_numbers=skip_line_numbers,
        )
    )


def _text_run(words: Sequence[Word], start: int, max_gap: float) -> str:
    parts = [words[start].text]
    for prev, cur in zip(words[start:], words[start + 1 :], strict=False):
        if cur.x0 - prev.x1 > max_gap:
            break
        parts.append(cur.text)
    return " ".join(parts)


def _same_row_text(row: Row, hit: LabelHit, max_gap: float) -> str | None:
    words = [w for w in _words_right_of(row, hit) if not _LEADER_ONLY.match(w.text)]
    if not words:
        return None
    return _text_run(words, 0, max_gap)


def _below_text(
    rows: tuple[Row, ...], hit: LabelHit, *, x_tol: float, max_rows: int, lines: int, max_gap: float
) -> str | None:
    lo, hi = hit.label_x0 - x_tol, hit.label_x1 + x_tol
    found: list[str] = []
    limit = hit.row_index + max_rows
    index = hit.row_index + 1
    while index < len(rows):
        ordered = rows[index].words
        starts = [i for i, w in enumerate(ordered) if _overlaps(w, lo, hi)]
        if starts:
            found.append(_text_run(ordered, starts[0], max_gap))
            if len(found) >= lines:
                break
        elif found or index >= limit:
            break
        index += 1
    return "\n".join(found) if found else None


def text_for_label(
    doc: ExtractedDoc,
    label_re: re.Pattern[str],
    *,
    mode: Mode = "auto",
    occurrence: int = 0,
    pages: Sequence[int] | None = None,
    lines: int = 1,
    x_tol: float = _X_TOL,
    max_gap: float = _MAX_TEXT_GAP,
) -> str | None:
    """Free text next to a label (payer name etc.): the contiguous run of words right of the
    label on the same row, or the run starting in the label's x-range on the row(s) below
    (`lines` rows, joined with ``"\\n"``). The result is passed through `tin.scrub`; `None` when
    nothing is found. In "auto" mode same_row wins when non-empty, which can pick up a
    neighbouring cell's label on IRS box layouts, so prefer ``mode="below"`` there."""
    rows_by_page = _page_rows(doc, pages)
    hits = _hits_in(rows_by_page, label_re)
    if occurrence < 0 or occurrence >= len(hits):
        return None
    hit = hits[occurrence]
    rows = rows_by_page[hit.page_number]
    text: str | None = None
    if mode in ("same_row", "auto"):
        text = _same_row_text(rows[hit.row_index], hit, max_gap)
    if text is None and mode in ("below", "auto"):
        text = _below_text(
            rows, hit, x_tol=x_tol, max_rows=_MAX_ROWS_BELOW, lines=max(1, lines), max_gap=max_gap
        )
    if text is None:
        return None
    cleaned = scrub(text).strip()
    return cleaned or None


# --------------------------------------------------------------------------------------------
# Splitting a document into per-form blocks
# --------------------------------------------------------------------------------------------


def _page_from_rows(page_number: int, rows: Sequence[Row]) -> PageText:
    text = "\n".join(r.text for r in rows)
    return PageText(
        page_number=page_number,
        text=text,
        words=tuple(w for r in rows for w in r.words),
        char_count=len(text),
    )


def split_blocks(
    doc: ExtractedDoc,
    start_re: re.Pattern[str],
    *,
    end_re: re.Pattern[str] | None = None,
    keep_preamble: bool = False,
) -> tuple[ExtractedDoc, ...]:
    """Split `doc` into one `ExtractedDoc` per form.

    A block starts at each row whose text matches `start_re` and extends (across pages) to just
    before the next start row. If `end_re` is given, a (non-start) row matching it ends the block
    early (that row is excluded) and later rows belong to no block until the next start. With
    `keep_preamble` the rows before the first start row (e.g. a "Tax Year 2024" banner) are
    prepended to the first block. Original `page_number`s and word coordinates are preserved;
    page text is rebuilt from the rows. Returns `()` when `start_re` never matches.
    """
    blocks: list[dict[int, list[Row]]] = []
    current: dict[int, list[Row]] | None = {} if keep_preamble else None
    preamble_open = keep_preamble
    for page in doc.pages:
        for row in cluster_rows(page.words):
            is_start = start_re.search(row.text) is not None
            if is_start:
                if preamble_open:
                    assert current is not None
                    blocks.append(current)
                    preamble_open = False
                else:
                    current = {}
                    blocks.append(current)
            elif end_re is not None and end_re.search(row.text) and not preamble_open:
                current = None
                continue
            if current is not None:
                current.setdefault(page.page_number, []).append(row)
    return tuple(
        ExtractedDoc(
            file_sha256=doc.file_sha256,
            page_count=len(block),
            pages=tuple(_page_from_rows(pn, rows) for pn, rows in sorted(block.items())),
            has_text_layer=doc.has_text_layer,
        )
        for block in blocks
    )


def merge_docs(blocks: Sequence[ExtractedDoc]) -> ExtractedDoc:
    """One `ExtractedDoc` holding the rows of all `blocks` (e.g. the pieces of a section that was
    split at a repeated running heading). Rows of the same page number are interleaved by
    their vertical position; page text is rebuilt from the rows."""
    if not blocks:
        raise ValueError("merge_docs needs at least one block")
    by_page: dict[int, list[Row]] = {}
    for block in blocks:
        for page in block.pages:
            by_page.setdefault(page.page_number, []).extend(cluster_rows(page.words))
    pages = tuple(
        _page_from_rows(pn, sorted(rows, key=lambda r: r.top))
        for pn, rows in sorted(by_page.items())
    )
    return ExtractedDoc(
        file_sha256=blocks[0].file_sha256,
        page_count=len(pages),
        pages=pages,
        has_text_layer=blocks[0].has_text_layer,
    )


def first_block(
    doc: ExtractedDoc, start_re: re.Pattern[str], end_re: re.Pattern[str] | None = None
) -> ExtractedDoc:
    """The first block of `split_blocks` (use for W-2s that repeat Copy B/C/2 on a page). If
    `start_re` never matches, returns `doc` unchanged."""
    blocks = split_blocks(doc, start_re, end_re=end_re)
    return blocks[0] if blocks else doc


first_page_block = first_block


# --------------------------------------------------------------------------------------------
# Presence and checkbox helpers
# --------------------------------------------------------------------------------------------


def doc_has(
    doc: ExtractedDoc, pattern: str | re.Pattern[str], *, pages: Sequence[int] | None = None
) -> bool:
    """True when `pattern` matches anywhere in the text of `pages` (default all). A `str` pattern
    is compiled case-insensitively."""
    regex = re.compile(pattern, re.IGNORECASE) if isinstance(pattern, str) else pattern
    wanted = None if pages is None else set(pages)
    return any(
        regex.search(page.text) is not None
        for page in doc.pages
        if wanted is None or page.page_number in wanted
    )


def checkbox_checked(
    doc: ExtractedDoc, label_re: re.Pattern[str], *, pages: Sequence[int] | None = None
) -> bool:
    """True when a check mark (``[X]``, ``(X)``, ``X`` in brackets, or a ballot-box glyph) sits
    immediately before or after a match of `label_re` on its row."""
    mark_before = re.compile(rf"{_MARK}\s*(?:{label_re.pattern})", label_re.flags)
    mark_after = re.compile(rf"(?:{label_re.pattern})\s*{_MARK}", label_re.flags)
    for rows in _page_rows(doc, pages).values():
        for row in rows:
            if mark_before.search(row.text) or mark_after.search(row.text):
                return True
    return False


def corrected_marker(doc: ExtractedDoc) -> bool:
    """True when the form is marked CORRECTED: ``[X] CORRECTED`` / ``CORRECTED [X]``, or a bare
    ``CORRECTED`` banner. An empty box (``[ ] CORRECTED``) or the printed prompt
    ``CORRECTED (if checked)`` without a mark is NOT corrected."""
    if checkbox_checked(doc, _CORRECTED_RE):
        return True
    for rows in _page_rows(doc, None).values():
        for row in rows:
            for match in _CORRECTED_RE.finditer(row.text):
                before = row.text[: match.start()]
                after = row.text[match.end() :]
                if re.search(rf"{_EMPTY_BOX}\s*$", before):
                    continue
                if re.match(r"\s*\(\s*if\s+checked\s*\)", after, re.IGNORECASE):
                    continue
                if re.match(rf"\s*{_EMPTY_BOX}", after):
                    continue
                return True
    return False
