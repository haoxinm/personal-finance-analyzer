"""Description normalization, redaction, and merchant-key derivation (§3.6, P1-C).

`description_raw` never leaves this module: it is parsed, redacted, and reduced to
`description_clean` (the only egress-eligible field, I1) and `merchant_key` (the cache key used by
`merchant_map`, A27). The pipeline below runs in the exact order specified by §3.6 — that order is
load-bearing (see the module docstring note on redacting digits before stripping boilerplate).

Every regex that does not depend on caller-supplied data (the FORBIDDEN patterns, the digit/email/
phone/reference-token patterns, the boilerplate prefixes) is compiled once at import time (budget:
< 50 microseconds per description, §6.6). The one pattern that *does* depend on caller-supplied
data — the PII-term alternation, built from `pii_terms` — is compiled by `core.pii.compile_redactor`, memoized per distinct term set, since callers pass the
same term list for every transaction in a batch.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import yaml

from personal_finance_analyzer.core.pii import (
    TOKEN_NAME,
    TOKEN_NUMBER,
    TOKEN_PHONE,
    TOKEN_REDACTED,
    compile_redactor,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------------
# Frozen contracts duplicated here, verbatim, from §3.7 (`classify/llm/egress.py`, owned by
# P1-D, dispatched in parallel). A13 requires the *same* FORBIDDEN patterns to run here (log a
# counter, never raise) and at the egress boundary (raise). Because P1-C and P1-D are separate
# `Owns:` trees developed concurrently, the list is a local copy rather than a cross-lane import;
# both copies must stay identical to §3.7 if either is ever revised.
# --------------------------------------------------------------------------------------------

_FORBIDDEN_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("long_digit_run", re.compile(r"\d{6,}")),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.\w+")),
    ("phone", re.compile(r"\+?\d[\d\-\(\) ]{8,}\d")),
    ("currency_amount", re.compile(r"\$\s?\d")),
    ("iso_date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
)

_forbidden_hit_count = 0


def forbidden_hit_count() -> int:
    """Total descriptions, across this process's lifetime, whose `description_clean` matched a
    §3.7 `FORBIDDEN` pattern (A13). Feeds an operational counter/alert, not a hard failure — the
    hard failure happens at the egress boundary in `classify/llm/egress.py`."""
    return _forbidden_hit_count


def _check_forbidden(description_clean: str) -> None:
    """A13: run the egress `FORBIDDEN` patterns over `description_clean` at normalization time.
    Logs at WARNING and increments a counter on a match; never raises. A match here means
    normalization itself has a bug — surfacing it at the source, rather than only at egress,
    catches it long before it could ever reach an LLM."""
    global _forbidden_hit_count
    for name, pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(description_clean):
            _forbidden_hit_count += 1
            logger.warning(
                "normalize: description_clean matched FORBIDDEN pattern %r after redaction "
                "(A13) — this indicates a normalization bug, not raising here",
                name,
            )
            return


# --------------------------------------------------------------------------------------------
# Redaction patterns (§3.6 step 2), compiled once.
# --------------------------------------------------------------------------------------------

_CARD_RUN_RE: Final = re.compile(r"\d{12,19}")
_NUM_RUN_RE: Final = re.compile(r"\d{6,}")
# Not one of §3.6's explicitly enumerated redaction kinds, but required by I1 ("No LLM ever
# receives ... dates, amounts ...") and by the A13 guarantee that FORBIDDEN's `currency_amount`
# pattern (§3.7: `\$\s?\d`) never fires against a correctly normalized description. A long
# dollar figure is already consumed by the digit-run rules above; this catches the short ones
# ("$9", "$1234.56") those rules do not reach.
_AMOUNT_RE: Final = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")
_EMAIL_RE: Final = re.compile(r"[\w.+-]+@[\w-]+\.\w+")
_PHONE_RE: Final = re.compile(r"\+?\d[\d\-\(\) ]{8,}\d")
_TRAILING_REF_RE: Final = re.compile(r"(?:^|\s)(?:REF#|AUTH|TRACE|ID:|INV)\S*\s*$", re.IGNORECASE)

# §3.6 step 3: boilerplate *prefixes* only. Longest first so a longer literal is not shadowed by
# a shorter one that happens to be a leading substring of it.
_BOILERPLATE_PREFIXES: Final[tuple[str, ...]] = tuple(
    sorted(
        (
            "SQ *",
            "TST*",
            "SP *",
            "PAYPAL *",
            "POS DEBIT",
            "PURCHASE AUTHORIZED ON",
            "DEBIT CARD PURCHASE",
            "RECURRING PAYMENT",
            "CHECKCARD",
        ),
        key=len,
        reverse=True,
    )
)

# §3.6 merchant-key derivation.
_STORE_NUMBER_HASH_RE: Final = re.compile(r"#\s*\d+")
_STORE_NUMBER_WORD_RE: Final = re.compile(r"\bstore\s*#?\.?\s*\d+\b")
_STORE_NUMBER_NO_RE: Final = re.compile(r"\bno\.?\s*\d+\b")
_REDACTION_TOKEN_RE: Final = re.compile(r"\[(?:amount|card|num|email|phone|name|number|redacted)\]")
_PUNCT_RE: Final = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE: Final = re.compile(r"\s+")

# Unicode categories stripped before anything else runs: control characters and format
# characters (which includes zero-width spaces/joiners and bidi override marks) — both are
# invisible and can otherwise be used to split a sensitive token across an otherwise-matching
# regex, or simply clutter descriptions with junk that has no informational value.
_STRIPPED_UNICODE_CATEGORIES: Final = frozenset({"Cc", "Cf"})


@dataclass(frozen=True, slots=True)
class Normalized:
    """The result of normalizing one `RawTransaction.description`."""

    description_clean: str
    merchant_key: str
    redaction_counts: dict[str, int] = field(default_factory=dict)


def normalize(description_raw: str, pii_terms: Sequence[str]) -> Normalized:
    """Derive `description_clean` and `merchant_key` from a raw transaction description
    (§3.6), in this exact order: NFKC-normalize and strip control/format characters, redact
    (counting substitutions), strip a leading boilerplate prefix, apply store aliases, then
    derive the merchant key.

    `description_raw` is read only by this function; the value it returns never contains it,
    and `description_clean` is the only field of the result that may later be sent to an LLM
    (I1). Non-Latin scripts are preserved unchanged throughout.

    Args:
        description_raw: the parser-produced, unredacted transaction description.
        pii_terms: configured PII terms (`[privacy] pii_terms` + `users.pii_aliases`, D10/A11),
            case-insensitive, word-boundary matched; terms shorter than 3 characters are ignored
            here too, so a caller need not pre-filter (A11's "Al" vs. "Aldi" example).

    Returns:
        A `Normalized` with `description_clean`, `merchant_key`, and `redaction_counts` (a count
        per redaction kind: `card`, `num`, `email`, `phone`, `name`, `ref`).
    """
    text = _strip_control_and_collapse_ws(unicodedata.normalize("NFKC", description_raw))
    text, counts = _redact(text, pii_terms)
    text = _strip_boilerplate_prefix(text)
    text = _apply_store_aliases(text)
    description_clean = _collapse_ws(text)

    _check_forbidden(description_clean)

    return Normalized(
        description_clean=description_clean,
        merchant_key=_merchant_key(description_clean),
        redaction_counts=counts,
    )


# --------------------------------------------------------------------------------------------
# Step 1 — NFKC normalize, strip control/format characters, collapse whitespace.
# --------------------------------------------------------------------------------------------


def _strip_control_and_collapse_ws(text: str) -> str:
    kept: list[str] = []
    for ch in text:
        if ch.isspace():
            kept.append(" ")
        elif unicodedata.category(ch) in _STRIPPED_UNICODE_CATEGORIES:
            continue
        else:
            kept.append(ch)
    return _collapse_ws("".join(kept))


def _collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


# --------------------------------------------------------------------------------------------
# Step 2 — redact, counting each substitution.
# --------------------------------------------------------------------------------------------


def _redact(text: str, pii_terms: Sequence[str]) -> tuple[str, dict[str, int]]:
    counts = {"amount": 0, "card": 0, "num": 0, "email": 0, "phone": 0, "name": 0, "ref": 0}

    def _sub_amount(_: re.Match[str]) -> str:
        counts["amount"] += 1
        return "[AMOUNT]"

    def _sub_card(_: re.Match[str]) -> str:
        counts["card"] += 1
        return "[CARD]"

    def _sub_num(_: re.Match[str]) -> str:
        counts["num"] += 1
        return "[NUM]"

    def _sub_email(_: re.Match[str]) -> str:
        counts["email"] += 1
        return "[EMAIL]"

    def _sub_phone(_: re.Match[str]) -> str:
        counts["phone"] += 1
        return "[PHONE]"

    # E-mail first (whole address, so a name entry cannot leave `[NAME]@host` behind), then the
    # user's own personal-info entries, so a phone/account entry is removed whole instead of
    # being half-masked by the generic digit rules below.
    text = _EMAIL_RE.sub(_sub_email, text)
    text, pii_counts = compile_redactor(pii_terms).redact_counts(text)
    counts["phone"] += pii_counts[TOKEN_PHONE]
    counts["num"] += pii_counts[TOKEN_NUMBER]
    counts["name"] += pii_counts[TOKEN_NAME] + pii_counts[TOKEN_REDACTED]

    # Dollar amounts first, so a short one ("$9") is not later mistaken for a bare digit run,
    # and a long one ("$1234567.89") does not leave a stray "$" once its digits are consumed.
    text = _AMOUNT_RE.sub(_sub_amount, text)
    # Digit runs: 12-19 digits -> [CARD] first, so a longer/shorter remaining run is not
    # double-counted; whatever remains that is still >= 6 digits -> [NUM].
    text = _CARD_RUN_RE.sub(_sub_card, text)
    text = _NUM_RUN_RE.sub(_sub_num, text)
    text = _EMAIL_RE.sub(_sub_email, text)
    # Phone-like separated digit sequences (also catches card numbers written with separators,
    # e.g. "4111-1111-1111-1111", which no longer look like one contiguous digit run above).
    text = _PHONE_RE.sub(_sub_phone, text)

    before = text
    text = _TRAILING_REF_RE.sub("", text).rstrip()
    if text != before:
        counts["ref"] += 1

    return text, counts


# --------------------------------------------------------------------------------------------
# Step 3 — strip a leading boilerplate prefix (never a suffix or interior occurrence).
# --------------------------------------------------------------------------------------------


def _strip_boilerplate_prefix(text: str) -> str:
    stripped = text.lstrip()
    lowered = stripped.lower()
    for prefix in _BOILERPLATE_PREFIXES:
        if lowered.startswith(prefix.lower()):
            return stripped[len(prefix) :].lstrip()
    return stripped


# --------------------------------------------------------------------------------------------
# Step 4 — store_aliases.yaml. Loaded once at import time; `yaml.safe_load` only (§4).
# --------------------------------------------------------------------------------------------

_STORE_ALIASES_PATH: Final = Path(__file__).resolve().parent.parent / "data" / "store_aliases.yaml"


def _load_store_aliases(path: Path) -> tuple[tuple[re.Pattern[str], str], ...]:
    raw: list[dict[str, str]] = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return tuple(
        (re.compile(str(entry["pattern"]), re.IGNORECASE), str(entry["canonical"])) for entry in raw
    )


_STORE_ALIASES: Final = _load_store_aliases(_STORE_ALIASES_PATH)


def _apply_store_aliases(text: str) -> str:
    """Replace the first matching aliased fragment with its canonical merchant name. At most
    one alias applies per description — a description names exactly one merchant."""
    for pattern, canonical in _STORE_ALIASES:
        if pattern.search(text):
            return pattern.sub(canonical, text, count=1)
    return text


# --------------------------------------------------------------------------------------------
# merchant_key derivation (§3.6, A27).
# --------------------------------------------------------------------------------------------


def _strip_store_numbers(key: str) -> str:
    key = _STORE_NUMBER_HASH_RE.sub(" ", key)
    key = _STORE_NUMBER_WORD_RE.sub(" ", key)
    key = _STORE_NUMBER_NO_RE.sub(" ", key)
    key = _collapse_ws(key)
    # A27: a *trailing* bare digit run is a store-number shape only when a non-digit token
    # precedes it (a "name" the number is appended to). A digit-only remainder ("76") or a
    # digit that is itself part of the name ("7 eleven", "99 ranch market") must survive.
    tokens = key.split(" ")
    if len(tokens) >= 2 and tokens[-1].isdigit() and any(not tok.isdigit() for tok in tokens[:-1]):
        key = " ".join(tokens[:-1])
    return key


def _strip_redaction_tokens(key: str) -> str:
    return _collapse_ws(_REDACTION_TOKEN_RE.sub(" ", key))


def _strip_punct_and_collapse(key: str) -> str:
    return _collapse_ws(_PUNCT_RE.sub(" ", key))


def _merchant_key(description_clean: str) -> str:
    key = description_clean.lower()
    key = _strip_store_numbers(key)
    key = _strip_redaction_tokens(key)
    key = _strip_punct_and_collapse(key)
    if len(key.replace(" ", "")) < 3:
        # A27 fallback: never let over-aggressive stripping collapse a short but legitimate
        # digit-named merchant key to something too short to be useful as a cache key.
        key = _strip_punct_and_collapse(description_clean.lower())
    return key
