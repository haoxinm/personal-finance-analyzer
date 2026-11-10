"""Shared helpers for tax parsers: year handling, result assembly, payer-name sanitising and the
"another 1099 section is present" detector used to halve single-form detect scores.

Helper module: exposes no object with `id`/`version`/`form_types`/`detect`/`parse`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.tax_types import (
    ParsedTaxDocument,
    ParsedTaxForm,
    UnparsedRegion,
)
from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest.tax.tin import contains_ssn_shape, has_long_digit_run, scrub
from personal_finance_analyzer.ingest.tax.year import detect_tax_year

MAX_PAYER_NAME_LEN = 120
MIN_TAX_YEAR = 2000
MAX_TAX_YEAR = 2099

_SECTION_RE = re.compile(r"\b(?:Form\s*)?1099-([A-Z]{1,4})\b")
_NINE_DIGITS = re.compile(r"(?:\d[\s-]*){9,}")
_HAS_LETTER = re.compile(r"[A-Za-z]")
_WS = re.compile(r"\s+")


_PII_TERMS: ContextVar[tuple[str, ...]] = ContextVar("tax_parse_pii_terms", default=())


@contextmanager
def parsing_with_pii_terms(terms: Sequence[str]) -> Iterator[None]:
    """Make the user's personal-info terms (`[privacy] pii_terms` + aliases) visible to the
    parsers while a document is parsed, so a parser never takes the recipient's own name for a
    payer. Scoped to the current thread / task; the parser signature stays unchanged."""
    token = _PII_TERMS.set(tuple(t for t in terms if t.strip()))
    try:
        yield
    finally:
        _PII_TERMS.reset(token)


def current_pii_terms() -> tuple[str, ...]:
    return _PII_TERMS.get()


#: What a parser says when the form prints no year (folder imports retry with the folder's year).
TAX_YEAR_UNKNOWN_MESSAGE = (
    "Could not determine the tax year for this form; provide the tax year explicitly."
)


def require_year(doc: ExtractedDoc, override: int | None) -> int:
    """The tax year to use: `override` if given, else `detect_tax_year(doc)`.

    Raises:
        ParserError: no year can be determined, or `override` is outside 2000-2099. The message
            is generic and contains no document text.
    """
    if override is not None:
        if not MIN_TAX_YEAR <= override <= MAX_TAX_YEAR:
            raise ParserError("The tax year provided is not valid.")
        return override
    year = detect_tax_year(doc)
    if year is None:
        raise ParserError(TAX_YEAR_UNKNOWN_MESSAGE)
    return year


def sanitize_payer_name(raw: str | None) -> str | None:
    """A payer name safe to store: whitespace collapsed, EIN/SSN shapes scrubbed, truncated to
    `MAX_PAYER_NAME_LEN`. Returns `None` for empty input or when the candidate is SSN-shaped
    (or consists only of nine or more digits: a misread identifier, not a name; an EIN embedded
    in a longer name is scrubbed instead)."""
    if raw is None:
        return None
    collapsed = _WS.sub(" ", raw).strip()
    if not collapsed:
        return None
    if contains_ssn_shape(collapsed) or (
        _NINE_DIGITS.search(collapsed) and not _HAS_LETTER.search(collapsed)
    ):
        return None
    cleaned = scrub(collapsed).strip()
    if len(cleaned) > MAX_PAYER_NAME_LEN:
        cleaned = cleaned[:MAX_PAYER_NAME_LEN].rstrip()
    return cleaned or None


def check_warning(message: str) -> str:
    """Return `message` unchanged, or raise `ValueError` if it holds a run of 6+ digits or an
    SSN/EIN shape. Programmer-error guard: warnings must be built from fixed text and short
    numbers only. (The error never echoes the message.)"""
    if has_long_digit_run(message) or scrub(message) != message:
        raise ValueError("warning text must not contain long digit runs or taxpayer identifiers")
    return message


def build_document(
    forms: Sequence[ParsedTaxForm],
    *,
    warnings: Iterable[str] = (),
    unparsed: Iterable[UnparsedRegion] = (),
) -> ParsedTaxDocument:
    """Assemble a `ParsedTaxDocument` from parsed forms.

    Enforces: at least one form; every form has the same `tax_year` (that year becomes the
    document's); `payer_tin_last4`/`account_last4` are exactly four digits or `None`; no warning
    contains a 6+ digit run or taxpayer-identifier shape. Document warnings are de-duplicated,
    order preserved.

    Raises:
        ParserError: no forms, or forms disagree on the tax year (generic message).
        ValueError: a privacy invariant above is violated (a parser bug).
    """
    if not forms:
        raise ParserError("No tax forms were found in this file.")
    years = {form.tax_year for form in forms}
    if len(years) != 1:
        raise ParserError("The forms in this file are for different tax years.")
    for form in forms:
        for value in (form.payer_tin_last4, form.account_last4):
            if value is not None and not re.fullmatch(r"\d{4}", value):
                raise ValueError("identifier last-four fields must be exactly four digits")
        for message in form.warnings:
            check_warning(message)
    doc_warnings = tuple(dict.fromkeys(check_warning(w) for w in warnings))
    return ParsedTaxDocument(
        tax_year=years.pop(),
        forms=tuple(forms),
        warnings=doc_warnings,
        unparsed=tuple(unparsed),
    )


@dataclass(frozen=True, slots=True)
class Collected:
    """Outcome of `collect`: the forms that parsed, plus a warning and an `UnparsedRegion` for each
    block that did not."""

    forms: tuple[ParsedTaxForm, ...]
    warnings: tuple[str, ...]
    unparsed: tuple[UnparsedRegion, ...]


def collect(
    blocks: Sequence[ExtractedDoc],
    parse_one: Callable[[ExtractedDoc], ParsedTaxForm | None],
    *,
    kind: str,
) -> Collected:
    """Parse each block independently so one unreadable form does not discard the others.

    `parse_one` returns a form, `None` (the block is not a form of this kind: ignored), or raises
    `ParserError`. Each `ParserError` becomes a warning (its generic message plus a fixed
    suffix) and an `UnparsedRegion(kind, pages)`. When EVERY block failed and nothing parsed, the
    first error is re-raised unchanged so a file with a single unreadable form still fails with
    that form's message.

    Raises:
        ParserError: every block raised (the first block's error).
    """
    forms: list[ParsedTaxForm] = []
    warnings: list[str] = []
    unparsed: list[UnparsedRegion] = []
    first_error: ParserError | None = None
    for block in blocks:
        try:
            form = parse_one(block)
        except ParserError as exc:
            first_error = first_error or exc
            message = str(exc).rstrip(".")
            warnings.append(
                check_warning(f"{message}; that form was skipped. Enter its figures manually.")
            )
            unparsed.append(
                UnparsedRegion(
                    kind=kind,
                    pages=tuple(p.page_number for p in block.pages),
                    reason="unreadable",
                )
            )
            continue
        if form is not None:
            forms.append(form)
    if not forms and first_error is not None:
        raise first_error
    return Collected(tuple(forms), tuple(dict.fromkeys(warnings)), tuple(unparsed))


def distinct_1099_sections(doc: ExtractedDoc) -> frozenset[str]:
    """Distinct 1099 section suffixes present as headings (``{"INT", "DIV", "B"}``); matches
    ``Form 1099-INT`` and bare ``1099-DIV``. Case-sensitive on the suffix (IRS headings are
    upper case)."""
    return frozenset(m.group(1) for m in _SECTION_RE.finditer(doc.full_text))


def other_1099_section_present(doc: ExtractedDoc, own: str | Collection[str]) -> bool:
    """True when a 1099 heading other than `own` (a suffix such as ``"INT"``, or a collection
    such as ``("NEC", "MISC")`` for a shared parser) appears. Single-form parsers multiply their
    detect score by 0.5 in that case so the consolidated parser wins."""
    own_set = {own.upper()} if isinstance(own, str) else {o.upper() for o in own}
    return any(section not in own_set for section in distinct_1099_sections(doc))


def halve_if_other_1099(score: float, doc: ExtractedDoc, own: str | Collection[str]) -> float:
    """`score * 0.5` when another 1099 section heading is present, else `score`."""
    return score * 0.5 if other_1099_section_present(doc, own) else score
