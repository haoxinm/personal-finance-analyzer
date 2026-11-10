"""Adversarial redaction corpus (P1-C, §3.6/§3.7).

This is a **security control**, not an ordinary unit test: it exists to catch the day someone
"simplifies" a regex and quietly reopens an egress hole. Every case asserts, together:

1. the sensitive fragment does not survive in `description_clean`;
2. none of the §3.7 `FORBIDDEN` patterns would fire against `description_clean` (mirroring the
   guard that raises at the egress boundary, A13); and
3. any non-Latin merchant text in the case is preserved intact (untouched by redaction).

At least 40 cases, covering: 16-digit card numbers with and without separators, emails, phone
numbers in several formats, the user's own name in varied casing, ISO and US dates, dollar
amounts, non-Latin scripts (Chinese, Japanese, Cyrillic, Arabic), RTL text, zero-width
characters, and combining marks.
"""

from __future__ import annotations

import re

import pytest

from personal_finance_analyzer.ingest.normalize import _FORBIDDEN_PATTERNS, normalize

PII_TERMS = ["John Doe", "Maria Garcia"]

# (raw description, forbidden fragments that must not survive, non-Latin fragment to preserve)
CASES: list[tuple[str, list[str], str | None]] = [
    # --- 16-digit card numbers, no separators ---
    ("CARD PMT 4111111111111111 STORE", ["4111111111111111"], None),
    ("VISA 4532015112830366 ONLINE ORDER", ["4532015112830366"], None),
    ("MC 5500005555555559 PURCHASE", ["5500005555555559"], None),
    ("AMEX 371449635398431 CHARGE", ["371449635398431"], None),
    # --- 16-digit card numbers, with separators ---
    ("CARD 4111-1111-1111-1111 STORE", ["4111-1111-1111-1111", "4111"], None),
    ("CARD 4111 1111 1111 1111 STORE", ["4111 1111 1111 1111"], None),
    ("CARD 4111-1111 1111-1111 STORE", ["4111-1111 1111-1111"], None),
    ("CARD (4111) 1111-1111-1111 STORE", ["1111-1111-1111"], None),
    # --- emails ---
    ("REFUND TO jane.doe@example.com", ["jane.doe@example.com", "@"], None),
    ("BILLED billing+dept@sub.corp.co.uk", ["billing+dept@sub.corp.co.uk", "@"], None),
    ("CONTACT SUPPORT@MERCHANT.IO", ["SUPPORT@MERCHANT.IO", "@"], None),
    ("user_name123@service-provider.net FEE", ["user_name123@service-provider.net", "@"], None),
    # --- phone numbers, several formats ---
    ("CALL 206-555-0199 FOR HELP", ["206-555-0199"], None),
    ("CALL (206) 555-0199 FOR HELP", ["(206) 555-0199"], None),
    ("CALL +1 206 555 0199 FOR HELP", ["206 555 0199"], None),
    ("CALL 1 206 555 0199 FOR HELP", ["206 555 0199"], None),
    ("SUPPORT LINE 8005551234", ["8005551234"], None),
    # --- user's own name, varied casing ---
    ("JOHN DOE PERSONAL TRANSFER", ["JOHN DOE"], None),
    ("john doe personal transfer", ["john doe"], None),
    ("John Doe Personal Transfer", ["John Doe"], None),
    ("JoHn DoE ZELLE PAYMENT", ["JoHn DoE"], None),
    ("MARIA GARCIA VENMO CASHOUT", ["MARIA GARCIA"], None),
    ("Wire from Maria Garcia re rent", ["Maria Garcia"], None),
    # --- ISO and US dates ---
    ("PURCHASE AUTHORIZED ON 2026-09-15 MERCHANT", ["2026-09-15"], None),
    ("STATEMENT DATE 2025-12-31 CLOSING", ["2025-12-31"], None),
    ("TXN ON 09/15/2026 AT MERCHANT", [], None),
    # --- dollar amounts ---
    ("TOTAL DUE $1234.56 THIS MONTH", ["$1234", "1234.56"], None),
    ("BALANCE $9 REMAINING", ["$9"], None),
    ("AMOUNT: $ 42.00 CHARGED", ["$ 42"], None),
    # --- non-Latin scripts (must be preserved) ---
    ("亚米网 YAMIBUY ORDER #123", [], "亚米网"),
    ("東京レストラン TOKYO RESTAURANT", [], "東京レストラン"),
    ("Кафе Пушкин MOSCOW", [], "Кафе Пушкин"),
    ("مطعم الشرق DAMASCUS", [], "مطعم الشرق"),
    ("한국식당 SEOUL EATERY", [], "한국식당"),
    # --- RTL text mixed with sensitive data ---
    ("مطعم الشرق CARD 4111111111111111", ["4111111111111111"], "مطعم الشرق"),
    ("حساب jane.doe@example.com الشرق", ["jane.doe@example.com", "@"], "الشرق"),
    # --- zero-width characters used to try to evade regexes ---
    ("CARD​4111​1111​1111​1111​END", ["4111"], None),
    ("john​doe​ personal transfer", [], None),
    ("jane.doe​@example.com PAYMENT", ["@example.com"], None),
    # --- combining marks (must not break redaction or crash) ---
    ("café 4111111111111111 order", ["4111111111111111"], None),
    ("résumé jane.doe@example.com", ["jane.doe@example.com", "@"], None),
    # --- mixed adversarial: sensitive data embedded in non-Latin merchant text ---
    ("亚米网 CALL 206-555-0199 客服", ["206-555-0199"], "亚米网"),
    ("東京レストラン john.doe@example.com", ["john.doe@example.com", "@"], "東京レストラン"),
]


def _assert_forbidden_would_not_fire(description_clean: str) -> None:
    for name, pattern in _FORBIDDEN_PATTERNS:
        assert not pattern.search(description_clean), (
            f"FORBIDDEN pattern {name!r} ({pattern.pattern!r}) still matches "
            f"{description_clean!r} after normalization"
        )


@pytest.mark.parametrize(
    ("raw", "must_not_contain", "must_preserve"),
    CASES,
    ids=[case[0] for case in CASES],
)
def test_corpus_case(raw: str, must_not_contain: list[str], must_preserve: str | None) -> None:
    result = normalize(raw, pii_terms=PII_TERMS)

    for fragment in must_not_contain:
        assert fragment not in result.description_clean, (
            f"sensitive fragment {fragment!r} survived normalization of {raw!r}: "
            f"{result.description_clean!r}"
        )

    _assert_forbidden_would_not_fire(result.description_clean)

    if must_preserve is not None:
        assert must_preserve in result.description_clean, (
            f"non-Latin text {must_preserve!r} was altered by normalization of {raw!r}: "
            f"{result.description_clean!r}"
        )


def test_corpus_has_at_least_40_cases() -> None:
    assert len(CASES) >= 40


def test_no_case_produces_a_bare_16_digit_run() -> None:
    long_digit_run_re = re.compile(r"\d{6,}")
    for raw, *_rest in CASES:
        result = normalize(raw, pii_terms=PII_TERMS)
        assert not long_digit_run_re.search(result.description_clean), (
            f"{raw!r} -> {result.description_clean!r} still has a long digit run"
        )
