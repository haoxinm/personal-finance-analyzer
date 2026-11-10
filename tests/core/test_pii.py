"""`core.pii`: the user's personal-info entries (names, phone/account numbers) and their matching
rules. Pure; entry values come from the test, never from a real person."""

from __future__ import annotations

import pytest

from personal_finance_analyzer.core.pii import (
    classify_entry,
    compile_redactor,
    merge_entries,
    parse_aliases,
)

PHONE_FORMATS = [
    "1234567890",
    "(123)4567890",
    "(123)456-7890",
    "(123)-456-7890",
    "(123) 456-7890",
    "123.456.7890",
    "123 456 7890",
    "123-456-7890",
    "+1 123-456-7890",
    "+1123-456-7890",
    "1-123-456-7890",
    "1 (123) 456-7890",
    "11234567890",
]


def _redact(entries: list[str], text: str) -> tuple[str, int]:
    return compile_redactor(entries).redact(text)


class TestClassify:
    @pytest.mark.parametrize(
        "value",
        [
            "(312) 555-0100",
            "3125550100",
            "+1 312.555.0100",
            "1234",
            "123-45-6789",
            "4111 1111 1111",
        ],
    )
    def test_digits(self, value: str) -> None:
        assert classify_entry(value) == "digits"

    @pytest.mark.parametrize(
        "value", ["Jane Doe", "123 Main St", "123", "ab-1234", "Unit 4B, 12-34", "", "  "]
    )
    def test_text(self, value: str) -> None:
        assert classify_entry(value) == "text"


class TestPhones:
    @pytest.mark.parametrize("entry", PHONE_FORMATS)
    @pytest.mark.parametrize("text_form", PHONE_FORMATS)
    def test_every_format_matches_every_other(self, entry: str, text_form: str) -> None:
        out, count = _redact([entry], f"call {text_form} now")
        assert out == "call [PHONE] now", (entry, text_form, out)
        assert count == 1

    def test_not_inside_longer_digit_run(self) -> None:
        for text in ("91234567890", "12345678901", "1234567890123", "x 0123456789"):
            assert _redact(["1234567890"], text) == (text, 0)

    def test_eleven_digit_entry_matches_ten_digit_form(self) -> None:
        assert _redact(["1-123-456-7890"], "tel 123.456.7890") == ("tel [PHONE]", 1)

    def test_parenthesis_is_consumed(self) -> None:
        assert _redact(["123-456-7890"], "(123) 456-7890") == ("[PHONE]", 1)

    def test_other_numbers_untouched(self) -> None:
        text = "123-456-7891 paid $1,234.56 on 2026-01-15"
        assert _redact(["123-456-7890"], text) == (text, 0)

    def test_generic_masking_never_sees_the_digits(self) -> None:
        out, _ = _redact(["(312) 555-0100"], "Tel 312-555-0100 ext")
        assert "0100" not in out


class TestAccountNumbers:
    @pytest.mark.parametrize(
        "text",
        ["4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111", "4111.1111.1111.1111"],
    )
    def test_any_grouping(self, text: str) -> None:
        assert _redact(["4111 1111 1111 1111"], f"Card {text}.") == ("Card [NUMBER].", 1)

    def test_ssn_like(self) -> None:
        assert _redact(["123-45-6789"], "SSN 123 45 6789 ok") == ("SSN [NUMBER] ok", 1)

    def test_short_number_not_inside_longer(self) -> None:
        assert _redact(["123-45-6789"], "9123456789") == ("9123456789", 0)


class TestNames:
    @pytest.mark.parametrize(
        "text",
        [
            "jane doe",
            "JANE DOE",
            "Jane   Doe",
            "Jane\nDoe",
            "Doe, Jane",
            "Doe Jane",
            "Jane Q. Doe",
            "Jane Q Doe",
            "DOE, JANE Q.",
        ],
    )
    def test_variants(self, text: str) -> None:
        assert _redact(["Jane Doe"], f"Payment from {text} received") == (
            "Payment from [NAME] received",
            1,
        )

    def test_entry_whitespace_and_comma_are_flexible(self) -> None:
        assert _redact(["Doe,  Jane"], "Jane Doe") == ("[NAME]", 1)

    def test_no_match_inside_longer_word(self) -> None:
        assert _redact(["Jan"], "Janet Jan's JAN") == ("Janet [REDACTED]'s [REDACTED]", 2)
        assert _redact(["Jane Doe"], "Jane Doeson") == ("Jane Doeson", 0)

    def test_single_token_and_address_use_redacted(self) -> None:
        assert _redact(["Springfield"], "in springfield")[0] == "in [REDACTED]"
        out, _ = _redact(["123 Main St."], "at 123 main st, Springfield")
        assert out == "at [REDACTED], Springfield"

    def test_three_token_name_reversed(self) -> None:
        assert _redact(["Mary Ann Smith"], "Smith, Mary Ann") == ("[NAME]", 1)

    def test_regex_metacharacters_are_literal(self) -> None:
        assert _redact(["a+b (c)"], "x a+b (c) y") == ("x [REDACTED] y", 1)


class TestCompile:
    def test_longest_first(self) -> None:
        out, count = _redact(["Jane", "Jane Doe"], "Jane Doe and Jane")
        assert out == "[NAME] and [REDACTED]"
        assert count == 2

    def test_replacement_tokens_are_not_rematched(self) -> None:
        # an entry spelled like a token must not eat the tokens other entries produced
        out, count = _redact(["Jane Doe", "name"], "Jane Doe")
        assert (out, count) == ("[NAME]", 1)

    def test_blank_and_short_entries_ignored(self) -> None:
        r = compile_redactor(["", "   ", "a", " x "])
        assert not r
        assert r.redact("a b x") == ("a b x", 0)

    def test_empty_text(self) -> None:
        assert _redact(["Jane Doe"], "") == ("", 0)

    def test_memoized_per_entry_tuple(self) -> None:
        assert compile_redactor(["Jane Doe", "123-456-7890"]) is compile_redactor(
            ("Jane Doe", "123-456-7890")
        )

    def test_counts_by_token(self) -> None:
        _, counts = compile_redactor(["Jane Doe", "123-456-7890"]).redact_counts(
            "Jane Doe 123.456.7890 jane doe"
        )
        assert counts["[NAME]"] == 2 and counts["[PHONE]"] == 1

    def test_failures_do_not_leak_values(self) -> None:
        # a pathological entry compiles (everything is escaped) and matches only itself
        assert _redact(["(((", "[[["], "((( [[[")[1] == 2


def test_merge_entries_dedupes_case_insensitively_and_drops_blanks() -> None:
    assert merge_entries(["Jane Doe", " ", "jane doe"], ["John", "Jane Doe"]) == (
        "Jane Doe",
        "John",
    )


def test_parse_aliases_is_forgiving() -> None:
    assert parse_aliases('["a", 1, "b"]') == ["a", "b"]
    assert parse_aliases("not json") == []
    assert parse_aliases('{"a": 1}') == []
    assert parse_aliases(None) == []
