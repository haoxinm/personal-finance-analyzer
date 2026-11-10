from __future__ import annotations

import pytest

from personal_finance_analyzer.ingest.tax.tin import (
    contains_ssn_shape,
    contains_tin_shape,
    has_long_digit_run,
    mask_tin,
    scrub,
    scrub_display_name,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("987-65-4321", "4321"),
        ("987654321", "4321"),
        ("99-0000001", "0001"),
        ("***-**-4321", "4321"),
        ("XXX-XX-4321", "4321"),
        ("123", None),
        ("abc", None),
        ("", None),
        (None, None),
    ],
)
def test_mask_tin(raw: str | None, expected: str | None) -> None:
    assert mask_tin(raw) == expected


def test_scrub_removes_every_ssn_and_ein_shape() -> None:
    text = "Name 912-34-5678 spouse 923 45 6789 raw 912345678 ein 99-0000001 ein2 990000002 ok 2024"
    out = scrub(text)
    assert "912" not in out
    assert "923" not in out
    assert "99-0000001" not in out
    assert not contains_tin_shape(out)
    assert "***-**-5678" in out
    assert "**-***0001" in out
    assert out.endswith("ok 2024")


def test_scrub_leaves_money_and_short_numbers_alone() -> None:
    text = "Wages 85,000.00 box 12a 4,321 year 2024 phone 1234567890"
    assert scrub(text) == text


def test_shape_predicates() -> None:
    assert contains_ssn_shape("x 912-34-5678 y")
    assert not contains_ssn_shape("99-0000001")
    assert contains_tin_shape("99-0000001")
    assert not contains_tin_shape("nothing here 12")


def test_has_long_digit_run() -> None:
    assert has_long_digit_run("a 123456 b")
    assert not has_long_digit_run("a 12345 b")
    assert has_long_digit_run("a 1234567 b", length=7)
    assert not has_long_digit_run("a 123456 b", length=7)


def test_scrub_display_name_masks_tins_and_long_digit_runs() -> None:
    out = scrub_display_name("W2 123-45-6789.pdf")
    assert out == "W2 ***-**-6789.pdf"
    assert scrub_display_name("acct 98765432101 w2.pdf") == "acct *******2101 w2.pdf"
    assert not has_long_digit_run(scrub_display_name("1234567890123 99-0000001 x"))
    assert scrub_display_name("w2-2024 final.pdf") == "w2-2024 final.pdf"
