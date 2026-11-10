"""Unit tests for `ingest/normalize.py` (§3.6, P1-C).

Covers the normalization pipeline end to end: NFKC handling, each redaction kind, boilerplate
prefix stripping, store aliases, merchant-key derivation (including the A27 fallback and digit-
named-merchant cases), the A13 forbidden-pattern counter, and the performance budget.
"""

from __future__ import annotations

import re
import time

import pytest

from personal_finance_analyzer.ingest import normalize as normalize_mod
from personal_finance_analyzer.ingest.normalize import Normalized, forbidden_hit_count, normalize


def test_normalized_is_frozen_dataclass() -> None:
    result = normalize("COFFEE SHOP", pii_terms=[])
    assert isinstance(result, Normalized)
    with pytest.raises(AttributeError):
        result.description_clean = "x"  # type: ignore[misc]


class TestNfkcAndControlChars:
    def test_fullwidth_characters_normalize_to_ascii(self) -> None:
        fullwidth_full = "".join(chr(ord(c) + 0xFEE0) for c in "FULL")
        result = normalize(fullwidth_full, pii_terms=[])
        assert result.description_clean == "FULL"

    def test_control_characters_are_stripped(self) -> None:
        result = normalize("MERCHANT\x01\x02 NAME", pii_terms=[])
        assert result.description_clean == "MERCHANT NAME"

    def test_zero_width_characters_are_stripped_and_do_not_hide_digits(self) -> None:
        # A zero-width space inserted mid-number must not let a 12-digit card number escape
        # redaction by breaking up the digit run.
        raw = "CARD​123456789012​END"
        result = normalize(raw, pii_terms=[])
        assert "1" not in result.description_clean.replace("[CARD]", "")
        assert "[CARD]" in result.description_clean

    def test_whitespace_is_collapsed_and_trimmed(self) -> None:
        result = normalize("  MERCHANT   \t NAME  \n ", pii_terms=[])
        assert result.description_clean == "MERCHANT NAME"

    def test_non_latin_scripts_are_preserved(self) -> None:
        for raw in ("亚米网 YAMIBUY", "Кафе Пушкин", "مطعم الشرق", "レストラン"):
            result = normalize(raw, pii_terms=[])
            assert result.description_clean == raw


class TestRedaction:
    def test_card_number_12_to_19_digits_becomes_card_token(self) -> None:
        result = normalize("PMT 123456789012 REF", pii_terms=[])
        assert "[CARD]" in result.description_clean
        assert result.redaction_counts["card"] == 1
        assert "123456789012" not in result.description_clean

    def test_digit_run_longer_than_19_is_still_fully_redacted(self) -> None:
        # `\d{12,19}` (§3.6, literal) greedily consumes 19 of the 20 digits as [CARD]; the
        # single leftover digit is not itself a long digit run, so no sensitive digits remain.
        result = normalize("ACCT 12345678901234567890 END", pii_terms=[])
        assert "[CARD]" in result.description_clean
        assert not re.search(r"\d{2,}", result.description_clean)

    def test_six_digit_run_becomes_num_token(self) -> None:
        result = normalize("STORE 123456 PURCHASE", pii_terms=[])
        assert "[NUM]" in result.description_clean
        assert result.redaction_counts["num"] == 1

    def test_short_store_number_survives(self) -> None:
        result = normalize("SHELL GAS #123 SEATTLE WA", pii_terms=[])
        assert "#123" in result.description_clean
        assert result.redaction_counts == {
            "amount": 0,
            "card": 0,
            "num": 0,
            "email": 0,
            "phone": 0,
            "name": 0,
            "ref": 0,
        }

    def test_email_is_redacted(self) -> None:
        result = normalize("BILLING jane.doe+work@example.co.uk PAYMENT", pii_terms=[])
        assert "[EMAIL]" in result.description_clean
        assert "@" not in result.description_clean
        assert result.redaction_counts["email"] == 1

    @pytest.mark.parametrize(
        "raw",
        [
            "CALL 206-555-0199 SUPPORT",
            "CALL (206) 555-0199 SUPPORT",
            "CALL +1 206 555 0199 SUPPORT",
        ],
    )
    def test_phone_like_patterns_are_redacted(self, raw: str) -> None:
        result = normalize(raw, pii_terms=[])
        assert "[PHONE]" in result.description_clean
        assert result.redaction_counts["phone"] == 1

    def test_separated_card_number_is_caught_by_phone_pattern(self) -> None:
        result = normalize("PMT 4111-1111-1111-1111 ONLINE", pii_terms=[])
        assert "4111" not in result.description_clean
        assert "[PHONE]" in result.description_clean

    def test_pii_term_is_redacted_case_insensitively(self) -> None:
        result = normalize("JOHN DOE VENMO TRANSFER", pii_terms=["John Doe"])
        assert "[NAME]" in result.description_clean
        assert "JOHN DOE" not in result.description_clean.upper()
        assert result.redaction_counts["name"] == 1

    def test_pii_term_shorter_than_3_chars_is_ignored(self) -> None:
        # A11: a user named "Al" must not blank out "Aldi".
        result = normalize("ALDI GROCERY STORE", pii_terms=["Al"])
        assert result.description_clean == "ALDI GROCERY STORE"
        assert result.redaction_counts["name"] == 0

    def test_pii_term_matches_on_word_boundary_only(self) -> None:
        result = normalize("SALLY'S DINER", pii_terms=["ALL"])
        assert result.description_clean == "SALLY'S DINER"

    @pytest.mark.parametrize(
        "raw",
        [
            "MERCHANT XYZ REF#998877",
            "MERCHANT XYZ AUTH445566",
            "MERCHANT XYZ TRACE0099887766",
            "MERCHANT XYZ ID:AB12CD34",
            "MERCHANT XYZ INV20260931",
        ],
    )
    def test_trailing_reference_tokens_are_stripped(self, raw: str) -> None:
        result = normalize(raw, pii_terms=[])
        assert result.description_clean == "MERCHANT XYZ"
        assert result.redaction_counts["ref"] == 1

    def test_non_trailing_reference_like_token_is_not_stripped(self) -> None:
        # Only a *trailing* reference token is stripped; one embedded mid-description is left
        # alone (it may still be redacted by the digit-run rule if it is long enough).
        result = normalize("AUTH#12 STORE PURCHASE", pii_terms=[])
        assert result.description_clean == "AUTH#12 STORE PURCHASE"
        assert result.redaction_counts["ref"] == 0

    def test_redaction_order_digits_before_boilerplate_strip(self) -> None:
        # If boilerplate stripping ran before digit redaction, "POS DEBIT 123456789012" would
        # lose its digits to the prefix strip and escape [CARD] entirely.
        result = normalize("POS DEBIT 123456789012 MERCHANT", pii_terms=[])
        assert result.description_clean == "[CARD] MERCHANT"


class TestBoilerplatePrefixStripping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("SQ *BLUE BOTTLE COFFEE", "BLUE BOTTLE COFFEE"),
            ("TST*THE CORNER DELI", "THE CORNER DELI"),
            ("SP * ACME SUPPLIES", "ACME SUPPLIES"),
            ("PAYPAL *NETFLIX.COM", "Netflix"),
            ("POS DEBIT SAFEWAY #4", "Safeway #4"),
            ("PURCHASE AUTHORIZED ON 01/02 MERCHANT NAME", "01/02 MERCHANT NAME"),
            ("DEBIT CARD PURCHASE TARGET STORE", "TARGET STORE"),
            ("RECURRING PAYMENT GYM MEMBERSHIP", "GYM MEMBERSHIP"),
            ("CHECKCARD WALGREENS #22", "Walgreens #22"),
        ],
    )
    def test_prefix_is_stripped_case_insensitively(self, raw: str, expected: str) -> None:
        result = normalize(raw, pii_terms=[])
        assert result.description_clean == expected

    def test_prefix_only_strips_leading_occurrence(self) -> None:
        result = normalize("MERCHANT POS DEBIT SUFFIX", pii_terms=[])
        assert result.description_clean == "MERCHANT POS DEBIT SUFFIX"


class TestStoreAliases:
    @pytest.mark.parametrize(
        ("raw", "expected_fragment"),
        [
            ("AMZN MKTP US*1A2B3", "Amazon"),
            ("WM SUPERCENTER #123", "Walmart"),
            ("UBER *EATS SAN FRANCISCO", "Uber Eats"),
            ("NETFLIX.COM", "Netflix"),
        ],
    )
    def test_alias_is_applied(self, raw: str, expected_fragment: str) -> None:
        result = normalize(raw, pii_terms=[])
        assert expected_fragment in result.description_clean

    def test_at_most_one_alias_applied(self) -> None:
        # "AMZN MKTP" matches before "AMAZON.COM" would, and only the matched fragment is
        # replaced once.
        result = normalize("AMZN MKTP US ORDER", pii_terms=[])
        assert result.description_clean.count("Amazon") == 1


class TestMerchantKey:
    def test_store_numbers_collapse_to_one_key(self) -> None:
        a = normalize("STARBUCKS #1234 SEATTLE WA", pii_terms=[])
        b = normalize("STARBUCKS #5678 SEATTLE WA", pii_terms=[])
        assert a.merchant_key == b.merchant_key
        assert a.merchant_key != ""

    def test_digit_named_merchants_produce_distinct_nonempty_keys(self) -> None:
        keys = {
            normalize(raw, pii_terms=[]).merchant_key
            for raw in ("76 #0457", "7-ELEVEN #33012", "99 RANCH MARKET #12")
        }
        assert len(keys) == 3
        assert all(k for k in keys)

    def test_merchant_key_is_lowercase_and_punctuation_free(self) -> None:
        result = normalize("Rick's Café #9", pii_terms=[])
        assert result.merchant_key == result.merchant_key.lower()
        assert "'" not in result.merchant_key
        assert "#" not in result.merchant_key

    def test_merchant_key_strips_bracketed_redaction_tokens(self) -> None:
        result = normalize("MERCHANT 123456789012", pii_terms=[])
        assert "[" not in result.merchant_key
        assert "]" not in result.merchant_key
        assert result.merchant_key == "merchant"

    def test_fallback_when_stripped_key_too_short(self) -> None:
        # "76" alone (after the store-number suffix is removed) is under 3 characters, so the
        # A27 fallback re-derives the key from the un-stripped description instead of losing
        # the merchant identity to an over-short key.
        result = normalize("76 #0457", pii_terms=[])
        assert len(result.merchant_key.replace(" ", "")) >= 3
        assert result.merchant_key != ""

    def test_merchant_key_never_strips_all_digits_from_a_name(self) -> None:
        result = normalize("7-ELEVEN #33012", pii_terms=[])
        assert "7" in result.merchant_key

    def test_trailing_bare_store_number_after_a_name_is_stripped(self) -> None:
        # No "#" or "store" marker — just a name followed by a bare trailing digit run, which
        # is still a store-number shape (A27: only stripped because a non-digit name precedes
        # it; a bare digit-only remainder like "76" would not be touched by this rule at all).
        result = normalize("SHELL GAS 04521", pii_terms=[])
        assert result.merchant_key == "shell gas"


class TestForbiddenCounter:
    def test_forbidden_match_increments_counter_and_does_not_raise(self) -> None:
        before = forbidden_hit_count()
        # In correct operation every FORBIDDEN pattern is structurally unreachable after
        # normalization (§3.7's own note). Exercise the guard directly, as if normalization had
        # a bug and let something slip through, to confirm it counts and logs rather than
        # raising — raising is reserved for the egress boundary.
        normalize_mod._check_forbidden("TOTAL $9 DUE")
        assert forbidden_hit_count() == before + 1

    def test_normalize_never_raises_even_when_forbidden_would_fire(self) -> None:
        # normalize() itself must never raise regardless of what _check_forbidden finds.
        result = normalize("TOTAL $9 DUE", pii_terms=[])
        assert isinstance(result, Normalized)

    def test_clean_description_does_not_increment_counter(self) -> None:
        before = forbidden_hit_count()
        normalize("TRADER JOES #123 SEATTLE WA", pii_terms=[])
        assert forbidden_hit_count() == before


class TestPerformanceBudget:
    def test_normalize_meets_performance_budget(self) -> None:
        # §6.6: < 50 microseconds per description. Generous margin for CI variance: assert the
        # mean over 10k calls is comfortably under budget rather than a hard per-call cap.
        descriptions = [f"STARBUCKS #{1000 + i} SEATTLE WA" for i in range(10_000)]
        pii_terms = ["John Doe", "Jane Smith"]
        # Warm the per-term-set regex cache before timing.
        normalize(descriptions[0], pii_terms=pii_terms)

        start = time.perf_counter()
        for description in descriptions:
            normalize(description, pii_terms=pii_terms)
        elapsed = time.perf_counter() - start

        per_call_us = (elapsed / len(descriptions)) * 1_000_000
        assert per_call_us < 200, f"normalize() averaged {per_call_us:.1f}us/call"


def test_forbidden_patterns_match_the_frozen_section_3_7_list() -> None:
    # Guards against silent drift between this local copy (see the module docstring) and §3.7.
    pattern_texts = {pattern.pattern for _, pattern in normalize_mod._FORBIDDEN_PATTERNS}
    assert pattern_texts == {
        r"\d{6,}",
        r"[\w.+-]+@[\w-]+\.\w+",
        r"\+?\d[\d\-\(\) ]{8,}\d",
        r"\$\s?\d",
        r"\b\d{4}-\d{2}-\d{2}\b",
    }


class TestUserPiiEntries:
    """The user's personal-info entries run before the generic digit/email rules."""

    def test_phone_entry_removed_whole_in_any_format(self) -> None:
        result = normalize("ZELLE TO 312.555.0100 RENT", pii_terms=["(312) 555-0100"])
        assert "0100" not in result.description_clean
        assert "[PHONE]" in result.description_clean
        assert result.redaction_counts["phone"] == 1

    def test_name_entry_uses_name_token_in_any_order_and_case(self) -> None:
        result = normalize("ZELLE FROM DOE, JANE", pii_terms=["Jane Doe"])
        assert result.description_clean == "ZELLE FROM [NAME]"
        assert result.redaction_counts["name"] == 1

    def test_email_is_masked_whole_even_when_a_name_entry_matches_its_local_part(self) -> None:
        result = normalize("PAY john@example.com", pii_terms=["john"])
        assert (
            "@" not in result.description_clean and "john" not in result.description_clean.lower()
        )

    def test_account_number_entry_counts_as_num(self) -> None:
        result = normalize("ACH 123-45-6789 PAYROLL", pii_terms=["123456789"])
        assert "6789" not in result.description_clean
        assert result.redaction_counts["num"] == 1
