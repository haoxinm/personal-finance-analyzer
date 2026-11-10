"""User-entered personal info (PII) redaction: pure, no I/O, no logging.

The user lists exact personal details (their name, a relative's name, phone numbers, an address,
account/SSN-like numbers). `compile_redactor` turns that list into one `Redactor`; both LLM egress
paths (`llm/statement_parser.redact` and `ingest/normalize._redact`) apply it *before* any generic
masking, so a user entry is matched in full instead of being half-masked by a generic rule first.

Each entry is classified by `classify_entry`:

- ``"digits"`` — at least 4 digits and otherwise only phone/number punctuation. Matched on the
  digit sequence with any separators between digits (`(312) 555-0100` == `312.555.0100` ==
  `3125550100`), never inside a longer digit run, with US `+1` / `1` prefixes tolerated for
  10/11-digit numbers. Replaced by ``[PHONE]`` (10-11 digits) or ``[NUMBER]``.
- ``"text"`` — everything else. Case-insensitive, whitespace-flexible (`[\\s,.]+` between tokens),
  word-boundary anchored; a 2-3 word alphabetic entry is name-like and also matches "Last, First"
  order and an optional middle initial. Replaced by ``[NAME]`` (name-like) or ``[REDACTED]``.

Entry values are never logged or put in exception messages.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Literal

EntryKind = Literal["digits", "text"]

#: Entries shorter than this (after stripping) are ignored so a stray character cannot redact
#: everything.
MIN_ENTRY_LENGTH: Final = 2
_MIN_DIGITS: Final = 4

TOKEN_PHONE: Final = "[PHONE]"
TOKEN_NUMBER: Final = "[NUMBER]"
TOKEN_NAME: Final = "[NAME]"
TOKEN_REDACTED: Final = "[REDACTED]"

_DIGIT_ENTRY_PUNCT: Final = frozenset(" -.()+/")
_DIGIT_SEP: Final = r"[\s().+\-/]*"
_TEXT_SEP: Final = r"[\s,.]+"
_NAME_TOKEN_RE: Final = re.compile(r"^[^\W\d_]+(?:['\u2019-][^\W\d_]+)*$")
_INITIAL: Final = r"(?:[\s,.]+[^\W\d_]\.?)?"


def classify_entry(value: str) -> EntryKind:
    """``"digits"`` for a phone/account-style entry, ``"text"`` for everything else."""
    text = value.strip()
    digits = sum(ch.isdigit() for ch in text)
    if digits >= _MIN_DIGITS and all(ch.isdigit() or ch in _DIGIT_ENTRY_PUNCT for ch in text):
        return "digits"
    return "text"


def merge_entries(*sources: Iterable[str]) -> tuple[str, ...]:
    """Strip, drop blanks and dedupe (case-insensitively, keeping the first spelling)."""
    seen: set[str] = set()
    out: list[str] = []
    for source in sources:
        for raw in source:
            if not isinstance(raw, str):
                continue
            entry = raw.strip()
            key = entry.casefold()
            if entry and key not in seen:
                seen.add(key)
                out.append(entry)
    return tuple(out)


def parse_aliases(raw: str | None) -> list[str]:
    """`users.pii_aliases` (a JSON list) -> list of strings; malformed values yield []."""
    try:
        data = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, str)]


def _is_name_like(tokens: list[str]) -> bool:
    return 2 <= len(tokens) <= 3 and all(_NAME_TOKEN_RE.match(t) for t in tokens)


def _digits_pattern(entry: str) -> tuple[str, str]:
    digits = re.sub(r"\D", "", entry)
    core = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
    body = _DIGIT_SEP.join(core)
    if len(core) == 10:
        # US numbers: an optional `+1` / `1` country code (with separators) and an optional
        # opening parenthesis before the area code.
        pattern = rf"(?:(?<!\d)(?:\+\s*)?1{_DIGIT_SEP}|\(?(?<!\d)){body}(?!\d)"
    else:
        pattern = rf"\(?(?<!\d){body}(?!\d)"
    return pattern, TOKEN_PHONE if len(digits) in (10, 11) else TOKEN_NUMBER


def _text_pattern(entry: str) -> tuple[str, str]:
    tokens = [t for t in re.split(r"[\s,]+", entry.strip()) if t]
    # A trailing dot ("St.", "Jr.") is optional in the text; dots inside the entry are already
    # covered by the `[\s,.]+` token separator.
    cleaned = [t.rstrip(".") for t in tokens]
    cleaned = [t for t in cleaned if t]
    escaped = [re.escape(t) for t in cleaned]
    if tokens and tokens[-1].endswith(".") and escaped:
        escaped[-1] += r"\.?"
    forward = _TEXT_SEP.join(escaped)
    token = TOKEN_REDACTED
    alternatives = [forward]
    if _is_name_like(cleaned):
        token = TOKEN_NAME
        if len(escaped) == 2:
            alternatives = [f"{escaped[0]}{_INITIAL}{_TEXT_SEP}{escaped[1]}"]
        last_first = _TEXT_SEP.join([escaped[-1], *escaped[:-1]])
        alternatives.append(f"{last_first}{_INITIAL}")
    inner = "|".join(f"(?:{a})" for a in alternatives)
    return rf"(?i:(?<!\w)(?:{inner})(?!\w))", token


@dataclass(frozen=True, slots=True)
class Redactor:
    """A compiled set of entries. `redact` replaces every match in one pass (so a replacement
    token is never re-matched by a later entry)."""

    _pattern: re.Pattern[str] | None
    _tokens: tuple[str, ...]

    def __bool__(self) -> bool:
        return self._pattern is not None

    def redact_counts(self, text: str) -> tuple[str, Counter[str]]:
        """Redacted text plus a count per replacement token (e.g. ``{"[PHONE]": 2}``)."""
        counts: Counter[str] = Counter()
        pattern = self._pattern
        if pattern is None or not text:
            return text, counts

        def _sub(match: re.Match[str]) -> str:
            token = self._tokens[int(match.lastgroup[1:])] if match.lastgroup else TOKEN_REDACTED
            counts[token] += 1
            return token

        return pattern.sub(_sub, text), counts

    def redact(self, text: str) -> tuple[str, int]:
        """Redacted text and the number of replacements made."""
        out, counts = self.redact_counts(text)
        return out, sum(counts.values())


@lru_cache(maxsize=16)
def _compile(entries: tuple[str, ...]) -> Redactor:
    usable = sorted(
        (e for e in entries if len(e) >= MIN_ENTRY_LENGTH),
        key=len,
        reverse=True,
    )
    parts: list[str] = []
    tokens: list[str] = []
    for entry in usable:
        pattern, token = (
            _digits_pattern(entry) if classify_entry(entry) == "digits" else _text_pattern(entry)
        )
        parts.append(f"(?P<g{len(tokens)}>{pattern})")
        tokens.append(token)
    if not parts:
        return Redactor(None, ())
    return Redactor(re.compile("|".join(parts)), tuple(tokens))


def compile_redactor(entries: Iterable[str]) -> Redactor:
    """Compile (memoized per distinct entry tuple) a redactor; blank/1-char entries are ignored
    and the longest entries are tried first."""
    return _compile(merge_entries(entries))
