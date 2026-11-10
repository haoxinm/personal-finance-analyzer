"""Text normalisation for tax-form word boxes (deterministic, no I/O).

Real tax PDFs are noisy in ways that defeat label matching: broker statements print
``Form 1 0 9 9 - D I V`` letter-spaced, fill the gap between a label and its amount with a dot
leader, leave ``(cid:N)`` glyph placeholders where a font has no mapping, and repeat a page header
(account, taxpayer, ``Page 2 of 11``) on every page. This module cleans all of that once, before
any parser looks at the document, so the parsers' label regexes stay simple and generic.

`normalize_doc(doc)` returns an `ExtractedDoc` with the same pages and the same coordinates:

* ``(cid:N)`` placeholders removed;
* dot / underscore leaders removed (also when glued to a word or an amount);
* runs of letter-spaced fragments joined back into words (word breaks recovered from the gaps);
* ``( 1,234.56 )`` and ``- 1,234.56`` re-glued so the sign survives;
* instruction / legal text dropped (everything below an ``Instructions for Recipient`` heading,
  and later pages that carry no amount), because it mentions form names but has no figures;
* the repeated page header block (the rows around a ``Page n of m`` counter) removed from every
  page after the first, unless its content changed (a new account starts there).

A page that needs no change keeps its original `PageText` object, so a clean document is returned
unchanged (``normalize_doc(doc) is doc``). Never raises on odd input.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from itertools import pairwise

from personal_finance_analyzer.core.types import ExtractedDoc, PageText, Word
from personal_finance_analyzer.ingest.layout import cluster_rows, parse_money

_CID = re.compile(r"\(cid:\d+\)")
_LEADER_ONLY = re.compile(r"^[.·…_]+$|^-{3,}$|^={3,}$|^[.·…_\-=]{3,}$")
_LEADING_LEADER = re.compile(r"^[.·…_]{3,}(?=[^.·…_\s])")
_TRAILING_LEADER = re.compile(r"(?<=[^.·…_\s])[.·…_]{3,}$")

#: Longest gap (points) between two fragments of one letter-spaced run.
_MAX_FRAGMENT_GAP = 10.0
#: Fewest fragments that make a letter-spaced run ("T o t a l"; shorter ones are just letters).
_MIN_RUN = 5
_MIN_PAGE_HEIGHT = 792.0
_FRAGMENT = re.compile(r"^[A-Za-z0-9&/\-.,'\u2019%$]{1,2}$")
_MONEY_BODY = re.compile(r"^\$?\(?-?\$?\d[\d,]*(?:\.\d+)?\)?-?$")

_INSTRUCTIONS = re.compile(
    r"\bINSTRUCTIONS?\s+FOR\s+(?:THE\s+)?(?:RECIPIENT|EMPLOYEE|PAYER|FILER|TAXPAYER)\b"
    r"|\bNOTICE\s+TO\s+(?:RECIPIENT|EMPLOYEE)S?\b",
    re.IGNORECASE,
)
_PAGE_COUNTER = re.compile(r"\b\d{1,3}\s+of\s+\d{1,3}\b", re.IGNORECASE)
_AMOUNT_SHAPE = re.compile(r"^\(?-?\$?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?\)?-?$|\d\.\d{2}\)?-?$|^\$\d")


# --------------------------------------------------------------------------------------------
# Word level
# --------------------------------------------------------------------------------------------


def _shrunk(word: Word, text: str, *, keep_left: bool) -> Word:
    """`word` with new `text`, its box shrunk proportionally on the side that lost characters."""
    old = max(1, len(word.text))
    width = (word.x1 - word.x0) * len(text) / old
    if keep_left:
        return Word(text, word.x0, word.x0 + width, word.top, word.bottom)
    return Word(text, word.x1 - width, word.x1, word.top, word.bottom)


_MIDDLE_LEADER = re.compile(r"(?<=[^.\u00b7\u2026_\s])[.\u00b7\u2026_]{3,}(?=[^.\u00b7\u2026_\s])")


def _split_glued_leader(word: Word) -> list[Word]:
    """``Dividends.....1,234.56`` (a leader with no spaces around it) -> two words."""
    match = _MIDDLE_LEADER.search(word.text)
    if match is None:
        return [word]
    width = (word.x1 - word.x0) / max(1, len(word.text))
    left = Word(
        word.text[: match.start()], word.x0, word.x0 + width * match.start(), word.top, word.bottom
    )
    right = Word(
        word.text[match.end() :], word.x0 + width * match.end(), word.x1, word.top, word.bottom
    )
    return [left, *_split_glued_leader(right)]


def _clean_word(word: Word) -> Word | None:
    text = _CID.sub("", word.text)
    if not text.strip() or _LEADER_ONLY.match(text.strip()):
        return None
    keep_left = text == word.text or not word.text.startswith("(cid:")
    trimmed = _LEADING_LEADER.sub("", text)
    if trimmed != text:
        keep_left = False
        text = trimmed
    trimmed = _TRAILING_LEADER.sub("", text)
    if trimmed != text:
        keep_left = True
        text = trimmed
    if text == word.text:
        return word
    return _shrunk(word, text, keep_left=keep_left)


def _is_fragment(word: Word) -> bool:
    return _FRAGMENT.match(word.text) is not None


def _is_alphabet_run(run: Sequence[Word]) -> bool:
    """``a b c d e``: consecutive letters are an enumeration, not a spaced-out word."""
    letters = [w.text for w in run]
    return all(len(t) == 1 and t.isalpha() for t in letters) and all(
        ord(b.lower()) - ord(a.lower()) == 1 for a, b in pairwise(letters)
    )


def _looks_letter_spaced(run: Sequence[Word]) -> bool:
    if _is_alphabet_run(run):
        return False
    singles = sum(1 for w in run if len(w.text) == 1)
    letters = sum(1 for w in run if any(c.isalpha() for c in w.text))
    if letters >= 3 and singles >= 3:
        return True
    # a spaced-out form number ("1 0 9 9 - B") has digits and a hyphen but few letters
    return singles == len(run) and len(run) >= 5 and any(not w.text.isdigit() for w in run)


def _join_run(run: Sequence[Word]) -> list[Word]:
    """Join a letter-spaced run into words, breaking where the gap is clearly a word space."""
    gaps = [b.x0 - a.x1 for a, b in pairwise(run)]
    base = statistics.median(gaps) if gaps else 0.0
    widths = [(w.x1 - w.x0) / max(1, len(w.text)) for w in run]
    threshold = base + max(1.2, 0.5 * statistics.mean(widths))
    groups: list[list[Word]] = [[run[0]]]
    for gap, word in zip(gaps, run[1:], strict=True):
        if gap > threshold:
            groups.append([word])
        else:
            groups[-1].append(word)
    return [
        Word(
            "".join(w.text for w in g),
            g[0].x0,
            g[-1].x1,
            min(w.top for w in g),
            max(w.bottom for w in g),
        )
        for g in groups
    ]


def _is_spaced_form_suffix(out: Sequence[Word], run: Sequence[Word]) -> bool:
    """``1099`` followed by a letter-spaced ``- I N T``: too short for a generic run, but the
    preceding form number says what it is."""
    return (
        bool(out)
        and out[-1].text.endswith("1099")
        and len(run) >= 3
        and run[0].text == "-"
        and all(len(w.text) == 1 and w.text.isupper() for w in run[1:])
    )


def _despace(words: Sequence[Word]) -> list[Word]:
    out: list[Word] = []
    i = 0
    while i < len(words):
        j = i
        while (
            j < len(words)
            and _is_fragment(words[j])
            and (j == i or words[j].x0 - words[j - 1].x1 <= _MAX_FRAGMENT_GAP)
        ):
            j += 1
        run = words[i:j]
        if _is_spaced_form_suffix(out, run):
            out.append(
                Word(
                    "".join(w.text for w in run),
                    run[0].x0,
                    run[-1].x1,
                    min(w.top for w in run),
                    max(w.bottom for w in run),
                )
            )
            i = j
        elif len(run) >= _MIN_RUN and _looks_letter_spaced(run):
            out.extend(_join_run(run))
            i = j
        else:
            out.append(words[i])
            i += 1
    return out


def _is_money_body(text: str) -> bool:
    return _MONEY_BODY.match(text) is not None and any(c.isdigit() for c in text)


def _glue_form_suffix(out: Sequence[Word], cur: Word, nxt: Word | None) -> bool:
    """``1099`` followed by a detached ``-INT`` or ``- INT`` suffix (letter-spacing leftovers)."""
    if re.fullmatch(r"-[A-Z]{1,4}", cur.text):
        return cur.x0 - out[-1].x1 < 8
    return (
        cur.text == "-"
        and nxt is not None
        and re.fullmatch(r"[A-Z]{1,4}", nxt.text) is not None
        and cur.x0 - out[-1].x1 < 8
    )


def _glue(words: Sequence[Word]) -> list[Word]:
    """Re-attach a detached sign or parenthesis to its amount: ``( 1,234.56 )`` and ``- 5.00``."""
    out: list[Word] = []
    i = 0
    while i < len(words):
        cur = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else None
        after = words[i + 2] if i + 2 < len(words) else None
        if out and out[-1].text.endswith("1099") and _glue_form_suffix(out, cur, nxt):
            joined = out.pop()
            tail = [cur]
            if cur.text == "-" and nxt is not None:
                tail.append(nxt)
            i += len(tail)
            out.append(
                Word(
                    joined.text + "".join(w.text for w in tail),
                    joined.x0,
                    tail[-1].x1,
                    joined.top,
                    joined.bottom,
                )
            )
            continue
        if (
            cur.text == "("
            and nxt is not None
            and after is not None
            and after.text == ")"
            and _is_money_body(nxt.text)
            and nxt.x0 - cur.x1 < 8
            and after.x0 - nxt.x1 < 8
        ):
            out.append(Word(f"({nxt.text})", cur.x0, after.x1, cur.top, cur.bottom))
            i += 3
            continue
        if (
            cur.text in ("-", "\u2212", "\u2013")
            and nxt is not None
            and _is_money_body(nxt.text)
            and nxt.x0 - cur.x1 < 8
            and (not out or cur.x0 - out[-1].x1 > nxt.x0 - cur.x1 + 0.5)
        ):
            out.append(Word(f"-{nxt.text}", cur.x0, nxt.x1, min(cur.top, nxt.top), cur.bottom))
            i += 2
            continue
        out.append(cur)
        i += 1
    return out


# --------------------------------------------------------------------------------------------
# Page level
# --------------------------------------------------------------------------------------------


def _page_from_rows(page_number: int, rows: Sequence[Sequence[Word]]) -> PageText:
    lines = [" ".join(w.text for w in row) for row in rows if row]
    text = "\n".join(lines)
    return PageText(
        page_number=page_number,
        text=text,
        words=tuple(w for row in rows for w in row),
        char_count=len(text),
    )


def _has_amount(words: Sequence[Word]) -> bool:
    return any(_AMOUNT_SHAPE.search(w.text) and parse_money(w.text) is not None for w in words)


def _drop_instructions(pages: list[list[list[Word]]]) -> bool:
    """Cut instruction text out of `pages` (rows of words per page) in place. Returns whether
    anything was dropped."""
    changed = False
    seen_marker = False
    for rows in pages:
        if seen_marker and _is_prose_or_empty(rows):
            if rows:
                rows.clear()
                changed = True
            continue
        for index, row in enumerate(rows):
            if not _is_instructions_heading(row):
                continue
            later = next((j for j in range(index + 1, len(rows)) if _has_amount(rows[j])), None)
            # Amount rows below the heading (a form sharing the page) must survive: drop only
            # the heading and the prose directly under it; otherwise cut to the page end.
            del rows[index : len(rows) if later is None else later]
            seen_marker = True
            changed = True
            break
    return changed


_HEADING_MAX_WORDS = 12


def _is_instructions_heading(row: Sequence[Word]) -> bool:
    """A standalone heading row ("INSTRUCTIONS FOR RECIPIENT OF FORMS 1099"): short, no amount,
    starting with the phrase. Inline prose that merely mentions it, or a form title row that
    also carries a notice phrase, is not one."""
    if len(row) > _HEADING_MAX_WORDS or _has_amount(row):
        return False
    match = _INSTRUCTIONS.search(" ".join(w.text for w in row))
    return match is not None and not re.search(r"[A-Za-z]{3}", match.string[: match.start()])


def _is_prose_or_empty(rows: Sequence[Sequence[Word]]) -> bool:
    return not rows or not any(_has_amount(r) for r in rows)


def _counter_block(rows: Sequence[Sequence[Word]], tops: Sequence[float], height: float) -> range:
    """Indices of the page-header (or footer) block around the page-counter row, or an empty
    range: the counter row plus the rows contiguous with it (at most 4 rows in all)."""
    for index, row in enumerate(rows):
        text = " ".join(w.text for w in row)
        zone = tops[index] < 0.18 * height or tops[index] > 0.85 * height
        if not zone or _PAGE_COUNTER.search(text) is None:
            continue
        gaps = sorted(b - a for a, b in pairwise(tops) if b - a > 0.5) or [12.0]
        pitch = 1.8 * gaps[len(gaps) // 4]  # the usual line pitch, not the biggest gaps
        lo = hi = index
        while lo > 0 and hi - lo < 3 and tops[lo] - tops[lo - 1] <= pitch:
            lo -= 1
        while hi + 1 < len(rows) and hi - lo < 3 and tops[hi + 1] - tops[hi] <= pitch:
            hi += 1
        return range(lo, hi + 1)
    return range(0)


def _signature(rows: Sequence[Sequence[Word]], block: range) -> tuple[str, ...]:
    return tuple(
        re.sub(r"\s+", " ", _PAGE_COUNTER.sub("", " ".join(w.text for w in rows[i]))).strip()
        for i in block
    )


def _strip_running_headers(pages: list[list[list[Word]]], height: float) -> bool:
    """Remove the repeated header/footer block of pages 2..n whose content equals the previous
    page's. A block that differs (another account number, say) is kept."""
    blocks = [
        _counter_block(rows, [r[0].top for r in rows], height) if rows else range(0)
        for rows in pages
    ]
    if sum(1 for b in blocks if b) < 2:
        return False
    changed = False
    previous: tuple[str, ...] | None = None
    for rows, block in zip(pages, blocks, strict=True):
        if not block:
            previous = None
            continue
        signature = _signature(rows, block)
        if previous is not None and signature == previous:
            del rows[block.start : block.stop]
            changed = True
        previous = signature
    return changed


def normalize_doc(doc: ExtractedDoc) -> ExtractedDoc:
    """The normalised document (see the module docstring); `doc` itself when nothing changed."""
    page_rows: list[list[list[Word]]] = []
    original: list[list[list[Word]]] = []
    for page in doc.pages:
        rows = [list(row.words) for row in cluster_rows(page.words)]
        original.append([list(r) for r in rows])
        page_rows.append([r2 for r2 in (_normalize_row_words(r) for r in rows) if r2])
    height = max((w.bottom for p in doc.pages for w in p.words), default=0.0)
    _drop_instructions(page_rows)
    _strip_running_headers(page_rows, max(height, _MIN_PAGE_HEIGHT))
    new_pages: list[PageText] = []
    changed = False
    for page, rows, before in zip(doc.pages, page_rows, original, strict=True):
        if rows == before:
            new_pages.append(page)
            continue
        changed = True
        new_pages.append(_page_from_rows(page.page_number, rows))
    if not changed:
        return doc
    return ExtractedDoc(
        file_sha256=doc.file_sha256,
        page_count=doc.page_count,
        pages=tuple(new_pages),
        has_text_layer=doc.has_text_layer,
    )


def _normalize_row_words(words: list[Word]) -> list[Word]:
    pieces = [piece for w in words for piece in _split_glued_leader(w)]
    cleaned = [w for w in (_clean_word(w) for w in pieces) if w is not None]
    return _glue(_despace(cleaned))
