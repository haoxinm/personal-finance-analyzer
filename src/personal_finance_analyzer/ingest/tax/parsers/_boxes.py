"""Box-reading helpers shared by the W-2 and 1099 parsers (underscore module: never scanned by the
tax registry, and it exposes no parser-shaped object).

Two layouts are handled by the same calls:

* IRS box layout: small label, value on the row beneath it inside the label's x-range.
* Same-row layout (software-generated forms, consolidated 1099 sections): the value follows the
  label on its row, possibly after dot leaders, a ``$`` or a parenthetical such as
  ``(includes lines 1b, 5, 2e)``.

`box_money` tries the same-row reading first, but only accepts a value that directly follows the
label (a neighbouring cell's box number or label means "no value on this row"), then falls back to
the cell beneath. Nothing here ever returns text that has not been through `tin.scrub`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from personal_finance_analyzer.core.types import ExtractedDoc, PageText, Word
from personal_finance_analyzer.ingest.layout import ParsedMoney, Row, cluster_rows, parse_money
from personal_finance_analyzer.ingest.tax.fields import (
    compile_label,
    find_label,
    money_for_label,
    split_blocks,
)
from personal_finance_analyzer.ingest.tax.tin import mask_tin, scrub

_LEADER = re.compile(r"^[.·:_\-…=>]+$")
_SMALL_INT = re.compile(r"^\d{1,3}[A-Za-z]?$")
_STARTS_ALPHA = re.compile(r"^[A-Za-z]")
_HAS_LETTER = re.compile(r"[A-Za-z]")
_STATE_CODE = re.compile(r"^[A-Z]{2}$")
_WS = re.compile(r"\s+")

#: Boxes may be tall: payer-block lines can sit between a label and its value.
_MAX_ROWS_BELOW = 3

#: Wrapped continuation lines of the payer / employer address label.
LABEL_CONTINUATION = re.compile(
    r"street address|city or town|state or province|country|\bZIP\b|postal code|telephone|"
    r"and telephone|\bstate,",
    re.IGNORECASE,
)

PAYER_NAME_LABEL = compile_label(r"PAYER'?S\s+name")
PAYER_TIN_LABEL = compile_label(r"PAYER'?S\s+(?:TIN|federal\s+identification\s+number)")
RECIPIENT_TIN_LABEL = compile_label(r"RECIPIENT'?S\s+(?:TIN|identification\s+number)")
ACCOUNT_LABEL = compile_label(r"Account\s+(?:number|no\.?)")
FED_WITHHELD_LABEL = compile_label(r"Federal\s+income\s+tax\s+withheld")
STATE_TAX_WITHHELD_LABEL = compile_label(r"State\s+tax\s+withheld")

#: Generic spellings of the payer-name / payer-TIN row labels used by software-generated and
#: consolidated documents (``Payer: X``, ``Payer's name X``, ``Broker: X``, ``Payer EIN 99-...``).
#: Anchored at the start of a row; a bare ``Payer`` / ``Broker`` / ``Issuer`` needs its colon.
PAYER_NAME_LABELS = compile_label(
    r"^(?:Payer(?:['\u2019]?s)?(?:\s*/\s*Broker)?\s+name\b|Filer['\u2019]?s\s+name\b"
    r"|(?:Payer\s*/\s*Broker|Payer|Broker|Issuer)(?=\s*:))\s*:?"
)
PAYER_TIN_LABELS = compile_label(
    r"^(?:Payer(?:['\u2019]?s)?\s+(?:TIN|EIN|federal\s+identification\s+number|federal\s+ID)\b"
    r"|Payer\s+ID\b|EIN\b)\s*:?"
)

CORRECTED_WARNING = "Form is marked as corrected; it may replace a form imported earlier."
STATE_CODE_WARNING = "A state tax amount was found but its state code could not be read."
UNKNOWN_STATE = "XX"
MAX_JURISDICTION_LEN = 32


def _page_rows(doc: ExtractedDoc) -> dict[int, tuple[Row, ...]]:
    return {page.page_number: cluster_rows(page.words) for page in doc.pages}


def _overlaps(word: Word, lo: float, hi: float) -> bool:
    return word.x1 >= lo and word.x0 <= hi


def adjacent_money(
    doc: ExtractedDoc, label_re: re.Pattern[str], *, occurrence: int = 0
) -> ParsedMoney | None:
    """The money token that directly follows the label on its row (after leaders, ``$``, a state
    code and a parenthetical), or `None`. A neighbouring cell's box number or label ends the search."""
    hits = find_label(doc, label_re)
    if occurrence < 0 or occurrence >= len(hits):
        return None
    hit = hits[occurrence]
    row = _page_rows(doc)[hit.page_number][hit.row_index]
    words = [w for w in row.words if w.x0 >= hit.label_x1 - 0.5]
    i = 0
    while i < len(words):
        text = words[i].text
        money = parse_money(text)
        if money is not None:
            nxt = words[i + 1].text if i + 1 < len(words) else None
            if _SMALL_INT.match(text) and nxt is not None and _STARTS_ALPHA.match(nxt):
                return None  # the next cell's box number
            return money
        if _LEADER.match(text) or text == "$" or _STATE_CODE.match(text):
            i += 1
            continue
        if text.startswith("("):
            while i < len(words) and not words[i].text.endswith(")"):
                i += 1
            i += 1
            continue
        return None
    return None


def box_money(
    doc: ExtractedDoc, label_re: re.Pattern[str], *, occurrence: int = 0
) -> ParsedMoney | None:
    """Money for a labelled box in either layout (see module docstring); `None` if absent/blank."""
    money = adjacent_money(doc, label_re, occurrence=occurrence)
    if money is not None:
        return money
    return money_for_label(
        doc, label_re, mode="below", occurrence=occurrence, max_rows_below=_MAX_ROWS_BELOW
    )


def box_amount(doc: ExtractedDoc, label_re: re.Pattern[str], *, occurrence: int = 0) -> int | None:
    """Magnitude in minor units of `box_money`, or `None`."""
    money = box_money(doc, label_re, occurrence=occurrence)
    return None if money is None else money.magnitude_minor


def label_present(doc: ExtractedDoc, label_re: re.Pattern[str]) -> bool:
    """True when the label matches somewhere in a single row of the document."""
    return bool(find_label(doc, label_re))


def _run_from(words: Sequence[Word], start: int, max_gap: float) -> str:
    parts = [words[start].text]
    for prev, cur in zip(words[start:], words[start + 1 :], strict=False):
        if cur.x0 - prev.x1 > max_gap:
            break
        parts.append(cur.text)
    return " ".join(parts)


def text_below(
    doc: ExtractedDoc,
    label_re: re.Pattern[str],
    *,
    skip_re: re.Pattern[str] | None = None,
    occurrence: int = 0,
    max_rows: int = 5,
    x_tol: float = 6.0,
    max_gap: float = 8.0,
) -> str | None:
    """Text of the first row below the label that has words in the label's x-range, skipping rows
    whose in-range text matches `skip_re` (wrapped label lines). Scrubbed; `None` if not found."""
    hits = find_label(doc, label_re)
    if occurrence < 0 or occurrence >= len(hits):
        return None
    hit = hits[occurrence]
    rows = _page_rows(doc)[hit.page_number]
    lo, hi = hit.label_x0 - x_tol, hit.label_x1 + x_tol
    for offset in range(1, max_rows + 1):
        index = hit.row_index + offset
        if index >= len(rows):
            return None
        in_range = [w for w in rows[index].words if _overlaps(w, lo, hi)]
        if not in_range:
            continue
        joined = " ".join(w.text for w in in_range)
        if skip_re is not None and skip_re.search(joined):
            continue
        text = scrub(_run_from(rows[index].words, rows[index].words.index(in_range[0]), max_gap))
        return text.strip() or None
    return None


def tin_last4(doc: ExtractedDoc, label_re: re.Pattern[str]) -> str | None:
    """Last four digits of the identifier printed beside (same row) or beneath the label."""
    hits = find_label(doc, label_re)
    if hits:
        hit = hits[0]
        row = _page_rows(doc)[hit.page_number][hit.row_index]
        after = [w for w in row.words if w.x0 >= hit.label_x1 - 0.5]
        if after and re.fullmatch(r"[:#]?[0-9Xx*\-]{4,}", after[0].text):
            return mask_tin(after[0].text)
    text = text_below(doc, label_re, max_rows=2)
    if text is None:
        return None
    first = text.split()[0] if text.split() else ""
    return mask_tin(first) if re.search(r"\d{4}", first) else None


def _drop_parentheticals(words: Sequence[Word]) -> list[Word]:
    kept: list[Word] = []
    inside = False
    for word in words:
        if word.text.startswith("("):
            inside = True
        if not inside:
            kept.append(word)
        if word.text.endswith(")"):
            inside = False
    return kept


def account_last4(doc: ExtractedDoc) -> str | None:
    """Last four digits of the account number (same row after the label, else beneath it)."""
    hits = find_label(doc, ACCOUNT_LABEL)
    if not hits:
        return None
    hit = hits[0]
    row = _page_rows(doc)[hit.page_number][hit.row_index]
    after = _drop_parentheticals([w for w in row.words if w.x0 >= hit.label_x1 - 0.5])
    candidate = after[0].text.lstrip(":#") if after else ""
    if len(re.findall(r"\d", candidate)) < 4:
        below = text_below(doc, ACCOUNT_LABEL, max_rows=2, skip_re=re.compile(r"see instructions"))
        candidate = below.split()[0] if below and below.split() else ""
    digits = "".join(ch for ch in candidate if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else None


def money_rows_below(
    doc: ExtractedDoc,
    label_re: re.Pattern[str],
    *,
    occurrence: int = 0,
    max_rows: int = 3,
    x_tol: float = 12.0,
) -> list[tuple[Row, ParsedMoney]]:
    """The value rows stacked under a label (IRS state/local tables print one label row and one
    value row per state). Collects each row's money within the label's x-range; stops at the
    first row whose in-range text is a label (letters)."""
    hits = find_label(doc, label_re)
    if occurrence < 0 or occurrence >= len(hits):
        return []
    hit = hits[occurrence]
    rows = _page_rows(doc)[hit.page_number]
    lo, hi = hit.label_x0 - x_tol, hit.label_x1 + x_tol
    found: list[tuple[Row, ParsedMoney]] = []
    for offset in range(1, max_rows + 1):
        index = hit.row_index + offset
        if index >= len(rows):
            break
        in_range = [w for w in rows[index].words if _overlaps(w, lo, hi)]
        if not in_range:
            if found:
                break
            continue
        if any(parse_money(w.text) is None and _HAS_LETTER.search(w.text) for w in in_range):
            break
        money = next((m for w in in_range if (m := parse_money(w.text)) is not None), None)
        if money is not None:
            found.append((rows[index], money))
    return found


_BOX_NUMBER = re.compile(r"^\d{1,2}[a-d]?$", re.IGNORECASE)
_BOX_CODE = re.compile(r"^[A-Z]{1,2}$")


def code_amount_in_cell(
    doc: ExtractedDoc,
    label_re: re.Pattern[str],
    codes: frozenset[str],
    *,
    max_rows: int = 2,
    x_tol: float = 12.0,
    span: float = 120.0,
) -> tuple[str, int] | None:
    """``(code, amount_minor)`` of a W-2 box 12 line: the capital-letter code and the money that
    follows it, on the label's row (after it) or on the rows beneath, in the label's cell. A row
    that starts the next box (a box number such as ``12b`` in the cell's x-range) ends the
    search; only codes in `codes` count. `None` when the line is blank or has another code."""
    hits = find_label(doc, label_re)
    if not hits:
        return None
    hit = hits[0]
    rows = _page_rows(doc)[hit.page_number]
    lo, hi = hit.label_x0 - x_tol, hit.label_x1 + span
    for offset in range(max_rows + 1):
        index = hit.row_index + offset
        if index >= len(rows):
            break
        left = hit.label_x1 - 0.5 if offset == 0 else lo
        words = [w for w in rows[index].words if w.x1 >= left and w.x0 <= hi]
        if offset > 0 and any(
            _BOX_NUMBER.match(w.text) and w.x0 <= hit.label_x0 + x_tol for w in words
        ):
            break
        for i, word in enumerate(words[:-1]):
            money = parse_money(words[i + 1].text)
            if word.text in codes and _BOX_CODE.match(word.text) and money is not None:
                return word.text, money.magnitude_minor
    return None


def state_code_in(row: Row) -> str | None:
    """First two-capital-letter word on the row (the state abbreviation printed in the value row
    of a state table), or `None`."""
    for word in row.words:
        if _STATE_CODE.match(word.text):
            return word.text
    return None


def state_tax_lines(
    doc: ExtractedDoc, label_re: re.Pattern[str]
) -> list[tuple[str | None, ParsedMoney]]:
    """``(state code or None, amount)`` per state value row under the label, or the same-row
    value when the layout has one. Amounts are magnitudes."""
    rows = money_rows_below(doc, label_re)
    if rows:
        return [(state_code_in(row), money) for row, money in rows]
    money = adjacent_money(doc, label_re)
    if money is None:
        return []
    hits = find_label(doc, label_re)
    row = _page_rows(doc)[hits[0].page_number][hits[0].row_index]
    return [(state_code_in(row), money)]


def clean_jurisdiction(raw: str | None) -> str | None:
    """A locality/state label safe to store: scrubbed, whitespace collapsed, at most
    `MAX_JURISDICTION_LEN` characters; `None` when empty."""
    if raw is None:
        return None
    cleaned = _WS.sub(" ", scrub(raw)).strip()
    if not cleaned or not _HAS_LETTER.search(cleaned):
        return None
    return cleaned[:MAX_JURISDICTION_LEN].rstrip()


_CONTINUATION = r"\bcont(?:inued|'d)\b|\bpage\s+(?:[2-9]|\d{2,})\s+of\s+\d+\b"


def without_continuations(heading_re: re.Pattern[str]) -> re.Pattern[str]:
    """`heading_re`, but a row that also says "continued" / "page 2 of 3" is not a heading: it is
    the repeated running heading of a form that spans pages, not the start of a new form."""
    return re.compile(
        rf"^(?!.*(?:{_CONTINUATION}))(?=.*(?:{heading_re.pattern}))",
        heading_re.flags | re.IGNORECASE,
    )


def split_forms(doc: ExtractedDoc, heading_re: re.Pattern[str]) -> tuple[ExtractedDoc, ...]:
    """Split a file into one document per form for single-form 1099 parsers.

    Zero or one heading row: the whole document. Several headings, at most one per page: one
    document per heading page (following pages without a heading stay with the previous form).
    Several headings on one page: `split_blocks` at each heading row (rows above the first
    heading are kept with the first form). A heading row that says "continued" or "page 2 of 3"
    does not start a new form.
    """
    heading_re = without_continuations(heading_re)
    per_page = [
        sum(1 for row in cluster_rows(page.words) if heading_re.search(row.text))
        for page in doc.pages
    ]
    if sum(per_page) <= 1:
        return (doc,)
    if max(per_page) > 1:
        return split_blocks(doc, heading_re, keep_preamble=True) or (doc,)
    groups: list[list[PageText]] = []
    lead: list[PageText] = []  # pages before the first heading stay with the first form
    for page, count in zip(doc.pages, per_page, strict=True):
        if not groups and not count:
            lead.append(page)
            continue
        if count or not groups:
            groups.append(lead)
            lead = []
        groups[-1].append(page)
    return tuple(
        ExtractedDoc(
            file_sha256=doc.file_sha256,
            page_count=len(group),
            pages=tuple(group),
            has_text_layer=doc.has_text_layer,
        )
        for group in groups
    )


def payer_from_row(row_text: str) -> tuple[str | None, str | None]:
    """``(name, tin_last4)`` read from one row when it is a payer-name or payer-TIN row, else
    ``(None, None)``. The name is the raw (scrubbed) text after the label; the caller sanitises it."""
    match = PAYER_NAME_LABELS.search(row_text)
    if match is not None:
        rest = scrub(row_text[match.end() :]).strip(" :\t")
        return (rest or None), None
    match = PAYER_TIN_LABELS.search(row_text)
    if match is not None:
        return None, mask_tin(row_text[match.end() :])
    return None, None
