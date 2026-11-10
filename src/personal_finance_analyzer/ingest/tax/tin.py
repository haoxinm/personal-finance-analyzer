"""Taxpayer-identifier masking and scrubbing: the ONLY place raw SSN/ITIN/EIN shapes are read.

Parsers call `mask_tin` immediately on any identifier they read and keep only the last four
digits (D3). Any text that could be echoed (payer-name candidates, labels, warnings) goes through
`scrub` first. The SSN/EIN regexes live in this module and nowhere else.

This is a helper module: it deliberately exposes no object with `id`/`version`/`form_types`/
`detect`/`parse`.
"""

from __future__ import annotations

import re

# An SSN/ITIN shape: 3-2-4 digits, optionally separated by hyphens or spaces, not embedded in a
# longer digit run.
_SSN_LIKE = re.compile(r"(?<!\d)\d{3}[- ]?\d{2}[- ]?\d{4}(?!\d)")
# An EIN shape: 2-7 digits with an optional hyphen.
_EIN_LIKE = re.compile(r"(?<!\d)\d{2}-?\d{7}(?!\d)")
_LONG_DIGIT_RUN = re.compile(r"\d{6,}")
_NON_DIGIT = re.compile(r"\D")


def mask_tin(raw: str | None) -> str | None:
    """Return the last four digits of a taxpayer identifier, or `None`.

    Accepts any rendering (``912-34-5678``, ``99-0000001``, ``***-**-4321``, ``XXX-XX-4321``).
    Returns `None` when fewer than four digits are present. The raw value must never be stored,
    logged or put in an exception message by the caller.
    """
    if not raw:
        return None
    digits = _NON_DIGIT.sub("", raw)
    if len(digits) < 4:
        return None
    return digits[-4:]


def scrub(text: str) -> str:
    """Replace every SSN-shaped substring with ``***-**-####`` and every EIN-shaped substring
    with ``**-***####`` (``####`` being the last four digits). Safe to apply to any text that
    might be echoed into a warning, name or error."""

    def _ssn(match: re.Match[str]) -> str:
        return "***-**-" + _NON_DIGIT.sub("", match.group(0))[-4:]

    def _ein(match: re.Match[str]) -> str:
        return "**-***" + _NON_DIGIT.sub("", match.group(0))[-4:]

    return _EIN_LIKE.sub(_ein, _SSN_LIKE.sub(_ssn, text))


def contains_tin_shape(text: str) -> bool:
    """True when `text` contains an SSN- or EIN-shaped substring (before scrubbing)."""
    return bool(_SSN_LIKE.search(text) or _EIN_LIKE.search(text))


def contains_ssn_shape(text: str) -> bool:
    """True when `text` contains an SSN/ITIN-shaped substring (9 digits, 3-2-4 grouping)."""
    return bool(_SSN_LIKE.search(text))


def has_long_digit_run(text: str, *, length: int = 6) -> bool:
    """True when `text` contains `length` or more consecutive digits (the privacy rule for
    warnings and error messages is: no digit run of 6 or more)."""
    if length == 6:
        return bool(_LONG_DIGIT_RUN.search(text))
    return re.search(rf"\d{{{length},}}", text) is not None


def scrub_display_name(name: str) -> str:
    """Make a user-supplied file name safe to store and show: `scrub` it, then mask every run of
    six or more digits down to its last four (``****1234``), so no identifier-shaped number
    survives in `tax_documents.original_name`."""

    def _mask(match: re.Match[str]) -> str:
        run = match.group(0)
        return "*" * (len(run) - 4) + run[-4:]

    return _LONG_DIGIT_RUN.sub(_mask, scrub(name))
