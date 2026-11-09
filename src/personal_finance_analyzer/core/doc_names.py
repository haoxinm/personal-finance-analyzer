"""Readable, portable names for the PDF copies in the documents directory (pure, no disk access).

Layout (every component is sanitized by `clean_component`)::

    <Person>/Statements/<Institution>/<YYYY-MM> <Institution> <account label>.pdf
    <Person>/Statements/Unsorted/<date> Statement <8 hex>.pdf        (not parsed yet / no period)
    <Person>/Tax/<tax year>/<Form> - <Payer>.pdf                     (W-2 - Acme Corp.pdf, 1040.pdf)
    Shared/...                                                        (a row with no person)

What is deliberately *not* in a name: SSNs, TINs/EINs, and full account numbers. Free text coming
from a document or an account label is passed through `scrub_digits`, which turns any run of five
or more digits (also when split by single spaces or dashes) into ``x`` plus its last four (account
labels, where the app already shows the last four) or removes it (payers, people).

A name must be valid on Windows, macOS, Linux and SMB at once, so components lose ``<>:"/\\|?*``
and control characters, trailing dots and spaces, leading dots (no hidden files), Windows reserved
device names (CON, NUL, COM1...), are NFC-normalized, and are capped in characters and in UTF-8
bytes (ext4 allows 255 bytes per name; the cap leaves room for a `` (2)`` collision suffix).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from datetime import date

#: What a row with no person is filed under (the UI calls it "shared"/everyone).
SHARED = "Shared"
#: Where a statement without a parsed period/account is filed until it is parsed.
UNSORTED = "Unsorted"

#: Maximum characters / UTF-8 bytes of one path component, extension included.
MAX_COMPONENT_CHARS = 120
MAX_COMPONENT_BYTES = 200
PDF_SUFFIX = ".pdf"

_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
_SEPARATORS = re.compile(r"[/\\|:]")  # become a dash: "A/B" -> "A-B"
_DROPPED = re.compile(r'[<>"?*]')
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\ufeff]")
_SPACES = re.compile(r"\s+")
_DIGIT_RUN = re.compile(r"\d(?:[ -]?\d)+")
_MASK_DOTS = re.compile(r"\.{2,}|[•*·…]+")

_FORM_LABELS = {
    "w2": "W-2",
    "1099-int": "1099-INT",
    "1099-div": "1099-DIV",
    "1099-b": "1099-B",
    "1099-nec": "1099-NEC",
    "1099-misc": "1099-MISC",
    "1099-consolidated": "1099 Consolidated",
    "1040": "1040",
    "state-return": "State return",
}


def scrub_digits(text: str, *, keep_last4: bool) -> str:
    """Replace every run of five or more digits (single spaces/dashes between digits allowed, so
    ``123-45-6789`` and ``4111 1111 1111 1111`` count) by ``x`` + its last four digits, or by
    nothing when ``keep_last4`` is false. Shorter runs (a year, a 4-digit mask) are kept."""

    def _replace(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) < 5:
            return match.group(0)
        return f"x{digits[-4:]}" if keep_last4 else " "

    return _DIGIT_RUN.sub(_replace, text)


def _cap(text: str, chars: int, bytes_: int) -> str:
    text = text[:chars]
    while len(text.encode("utf-8")) > bytes_:
        text = text[:-1]
    return text


def clean_component(
    text: str | None,
    *,
    fallback: str,
    keep_last4: bool = False,
    max_chars: int = MAX_COMPONENT_CHARS,
    max_bytes: int = MAX_COMPONENT_BYTES,
) -> str:
    """One safe path component (no extension handling: callers add ``.pdf`` themselves after
    capping with `stem_budget`). Never empty: ``fallback`` stands in for nothing usable."""
    value = unicodedata.normalize("NFC", text or "")
    value = scrub_digits(value, keep_last4=keep_last4)
    value = _CONTROL.sub(" ", value)
    value = _SEPARATORS.sub("-", value)
    value = _DROPPED.sub("", value)
    value = _SPACES.sub(" ", value).strip()
    value = re.sub(r"\s*-(?:\s*-)+\s*", " - ", value)  # "A//B" / "A - - B" -> "A - B"
    value = value.lstrip(". ").rstrip(". ")
    value = _cap(value, max_chars, max_bytes).rstrip(". ")
    if not value:
        return fallback
    return _unreserved(value)


def _unreserved(value: str) -> str:
    """``value`` with a Windows device name (``CON``, ``NUL.txt``...) made harmless: those names
    are reserved whatever extension follows, so the underscore goes after the first word."""
    head, dot, rest = value.partition(".")
    if head.strip().casefold() in _RESERVED:
        return f"{head.rstrip()}_{dot}{rest}"
    return value


def stem_budget(extra: str = "") -> tuple[int, int]:
    """Characters/bytes left for a file stem once ``.pdf`` and ``extra`` (a collision suffix) are
    appended to it within the component caps."""
    reserved = PDF_SUFFIX + extra
    return (
        MAX_COMPONENT_CHARS - len(reserved),
        MAX_COMPONENT_BYTES - len(reserved.encode("utf-8")),
    )


def file_name(stem: str, suffix: str = "") -> str:
    """``<stem><suffix>.pdf`` with the stem trimmed so the name stays within the component caps.
    ``suffix`` is the collision marker (`` (2)``)."""
    chars, bytes_ = stem_budget(suffix)
    return _unreserved(_cap(stem, chars, bytes_).rstrip(". ")) + suffix + PDF_SUFFIX


def person_folder(name: str | None) -> str:
    """The top-level folder of a person (``Shared`` when the row has none)."""
    return clean_component(name, fallback=SHARED)


def _account_label(display_name: str, institution: str) -> str:
    """The account's display name without a leading institution (it is in the file name already):
    ``"Chase credit ...1234"`` -> ``"credit 1234"``. Never more than the last four digits."""
    label = _MASK_DOTS.sub(" ", display_name or "").strip()
    inst = (institution or "").strip()
    if inst and label.casefold().startswith(inst.casefold()):
        label = label[len(inst) :].strip(" -")
    return clean_component(label, fallback="", keep_last4=True)


def statement_path(
    *,
    person: str | None,
    institution: str | None,
    account_display_name: str | None,
    period_end: str | None,
    ingested_at: str | None,
    sha256: str,
) -> tuple[str, str]:
    """``(folder, file stem)`` of a statement, relative to the documents directory; the folder is
    a ``/``-joined string. The stem has no extension and no collision suffix (see `file_name`)."""
    who = person_folder(person)
    month = _month_of(period_end)
    inst = clean_component(institution, fallback="")
    if month is None:
        stamp = _date_of(ingested_at) or "undated"
        return f"{who}/Statements/{UNSORTED}", f"{stamp} Statement {sha256[:8]}"
    folder_inst = inst or "Unknown institution"
    label = _account_label(account_display_name or "", institution or "")
    stem = " ".join(part for part in (month, inst, label) if part)
    return f"{who}/Statements/{folder_inst}", stem


def form_label(form_type: str) -> str:
    return _FORM_LABELS.get(form_type, clean_component(form_type, fallback="Form"))


def tax_path(
    *,
    person: str | None,
    tax_year: int | None,
    forms: Sequence[tuple[str, str | None, bool]],
    ingested_at: str | None,
    sha256: str,
) -> tuple[str, str]:
    """``(folder, file stem)`` of a tax document. ``forms`` are ``(form type, payer, corrected)``
    of the forms read from the file, in document order. One payer -> ``W-2 - Acme Corp``; several
    forms of one payer -> ``1099-DIV, 1099-INT - Ally Bank``; several payers -> ``... - Multiple
    payers``; a corrected form adds `` (corrected)``; no forms -> ``Unrecognized - <date> <8 hex>``.
    """
    who = person_folder(person)
    year = str(tax_year) if tax_year else "Unknown year"
    folder = f"{who}/Tax/{year}"
    if not forms:
        stamp = _date_of(ingested_at) or "undated"
        return folder, f"Unrecognized - {stamp} {sha256[:8]}"
    types = list(dict.fromkeys(form_label(f[0]) for f in forms))
    payers = list(
        dict.fromkeys(
            clean_component(f[1], fallback="")
            for f in forms
            if f[1] and clean_component(f[1], fallback="")
        )
    )
    head = ", ".join(types)
    if len(payers) == 1:
        head = f"{head} - {payers[0]}"
    elif len(payers) > 1:
        head = f"{head} - Multiple payers"
    if any(f[2] for f in forms):
        head += " (corrected)"
    return folder, head


def _month_of(iso: str | None) -> str | None:
    parsed = _parse_date(iso)
    return f"{parsed.year:04d}-{parsed.month:02d}" if parsed else None


def _date_of(iso: str | None) -> str | None:
    parsed = _parse_date(iso)
    return parsed.isoformat() if parsed else None


def _parse_date(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return date.fromisoformat(iso[:10])
    except ValueError:
        return None


def collision_suffix(n: int) -> str:
    """The marker for the n-th candidate name: nothing for 1, `` (2)``, `` (3)``..."""
    return "" if n <= 1 else f" ({n})"
