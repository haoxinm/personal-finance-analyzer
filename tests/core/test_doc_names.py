"""The readable names of the documents folder: sanitization, collisions, unicode, reserved names,
and the rule that no SSN, TIN/EIN or full account number ever reaches a path."""

from __future__ import annotations

import re
import unicodedata

import pytest

from personal_finance_analyzer.core import doc_names
from personal_finance_analyzer.core.doc_names import (
    clean_component,
    collision_suffix,
    file_name,
    person_folder,
    scrub_digits,
    statement_path,
    tax_path,
)

SHA = "ab" * 32


def _statement(**overrides: object) -> tuple[str, str]:
    fields: dict[str, object] = {
        "person": "Alex",
        "institution": "Chase",
        "account_display_name": "Chase credit ...1234",
        "period_end": "2024-03-31",
        "ingested_at": "2024-04-02T10:00:00+00:00",
        "sha256": SHA,
    }
    fields.update(overrides)
    return statement_path(**fields)  # type: ignore[arg-type]


# --- sanitization --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Chase", "Chase"),
        ("AT&T / Verizon", "AT&T - Verizon"),
        ('What? "Bank" <1> * | x', "What Bank 1 - x"),
        ("a\\b:c", "a-b-c"),
        ("  lots   of\t\nspace  ", "lots of space"),
        ("trailing dots...", "trailing dots"),
        ("trailing space .", "trailing space"),
        (".hidden", "hidden"),
        ("..", "x"),
        ("", "x"),
        ("\x00\x1f\x7f", "x"),
        ("zero\u200bwidth", "zero width"),
    ],
)
def test_clean_component_strips_everything_a_filesystem_dislikes(raw: str, expected: str) -> None:
    assert clean_component(raw, fallback="x") == expected


@pytest.mark.parametrize("name", ["CON", "nul", "Prn", "AUX", "COM1", "lpt9", "con.txt", "NUL."])
def test_windows_reserved_names_are_avoided(name: str) -> None:
    cleaned = clean_component(name, fallback="x")
    assert cleaned.split(".")[0].casefold() not in {"con", "prn", "aux", "nul", "com1", "lpt9"}
    assert "_" in cleaned
    # As a file name too: "NUL.pdf" is reserved on Windows just like "NUL".
    assert file_name(name).split(".")[0].casefold() not in {
        "con",
        "prn",
        "aux",
        "nul",
        "com1",
        "lpt9",
    }


def test_names_that_merely_contain_a_reserved_word_are_untouched() -> None:
    assert clean_component("Concord Bank", fallback="x") == "Concord Bank"
    assert clean_component("COM10", fallback="x") == "COM10"


def test_unicode_is_nfc_normalized_and_kept() -> None:
    decomposed = unicodedata.normalize("NFD", "Zoë Müller")
    assert decomposed != "Zoë Müller"
    cleaned = clean_component(decomposed, fallback="x")
    assert cleaned == unicodedata.normalize("NFC", "Zoë Müller")
    assert clean_component("李小龍 銀行", fallback="x") == "李小龍 銀行"


def test_components_are_capped_in_characters_and_bytes() -> None:
    long = clean_component("A" * 500, fallback="x")
    assert len(long) == doc_names.MAX_COMPONENT_CHARS
    wide = clean_component("銀" * 500, fallback="x")  # 3 bytes each
    assert len(wide.encode()) <= doc_names.MAX_COMPONENT_BYTES
    # A file name stays within the caps with the extension and a collision suffix on top.
    name = file_name("B" * 500, collision_suffix(12))
    assert name.endswith(" (12).pdf")
    assert len(name) <= doc_names.MAX_COMPONENT_CHARS
    assert len(file_name("銀" * 500, " (2)").encode()) <= doc_names.MAX_COMPONENT_BYTES + 4


def test_collision_suffixes() -> None:
    assert collision_suffix(1) == ""
    assert collision_suffix(2) == " (2)"
    assert file_name("2024-03 Chase", collision_suffix(3)) == "2024-03 Chase (3).pdf"


# --- no identifiers in names --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "123-45-6789",
        "123 45 6789",
        "123456789",
        "12-3456789",
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "Acme 98765432101 Corp",
    ],
)
def test_long_digit_runs_never_survive(text: str) -> None:
    assert not re.search(r"\d{5}", clean_component(text, fallback="x"))
    assert not re.search(r"\d{5}", clean_component(text, fallback="x", keep_last4=True))
    dropped = clean_component(f"Acme {text}", fallback="x")
    last4 = re.sub(r"\D", "", text)[-4:]
    assert last4 not in dropped  # payers and people lose the digits entirely
    assert f"x{last4}" in clean_component(f"Acme {text}", fallback="x", keep_last4=True)


def test_short_numbers_are_kept() -> None:
    assert scrub_digits("credit 1234", keep_last4=False) == "credit 1234"
    assert scrub_digits("Fund 2024", keep_last4=False) == "Fund 2024"


def test_statement_account_labels_hold_at_most_the_last_four_digits() -> None:
    folder, stem = _statement(account_display_name="Chase checking 000123456789")
    assert "123456789" not in f"{folder}/{stem}"
    assert stem.endswith("x6789")
    _folder, stem = _statement(account_display_name="Chase credit ...1234")
    assert stem == "2024-03 Chase credit 1234"


def test_tax_names_never_contain_a_tin() -> None:
    folder, stem = tax_path(
        person="Alex",
        tax_year=2024,
        forms=[("w2", "Acme Corp 12-3456789", False)],
        ingested_at=None,
        sha256=SHA,
    )
    assert "3456789" not in f"{folder}/{stem}"
    assert stem == "W-2 - Acme Corp"


# --- statements ---------------------------------------------------------------------------------


def test_statement_layout() -> None:
    assert _statement() == ("Alex/Statements/Chase", "2024-03 Chase credit 1234")


def test_statement_label_does_not_repeat_the_institution() -> None:
    _, stem = _statement(account_display_name="Chase Sapphire Reserve")
    assert stem == "2024-03 Chase Sapphire Reserve"
    _, stem = _statement(account_display_name="Sapphire")
    assert stem == "2024-03 Chase Sapphire"
    _, stem = _statement(account_display_name=None)
    assert stem == "2024-03 Chase"


def test_statement_without_a_person_is_filed_under_shared() -> None:
    folder, _ = _statement(person=None)
    assert folder == "Shared/Statements/Chase"
    assert person_folder("") == "Shared"


def test_statement_without_a_period_goes_to_unsorted_with_a_hash_prefix() -> None:
    folder, stem = _statement(period_end=None)
    assert folder == "Alex/Statements/Unsorted"
    assert stem == "2024-04-02 Statement abababab"
    _folder, stem = _statement(period_end="garbage", ingested_at=None)
    assert stem == "undated Statement abababab"


def test_statement_with_unknown_institution() -> None:
    folder, stem = _statement(institution=None, account_display_name="credit ...1234")
    assert folder == "Alex/Statements/Unknown institution"
    assert stem == "2024-03 credit 1234"


def test_person_names_are_sanitized_too() -> None:
    folder, _ = _statement(person="Alex/../Evil: <b>")
    assert ".." not in folder.split("/")
    assert folder.count("/") == 2  # the person is one component, whatever it contained


# --- tax ----------------------------------------------------------------------------------------


def _tax(forms: list[tuple[str, str | None, bool]], year: int | None = 2024) -> tuple[str, str]:
    return tax_path(person="Alex", tax_year=year, forms=forms, ingested_at=None, sha256=SHA)


def test_tax_single_form() -> None:
    assert _tax([("w2", "Acme Corp", False)]) == ("Alex/Tax/2024", "W-2 - Acme Corp")
    assert _tax([("1099-int", "Ally Bank", False)])[1] == "1099-INT - Ally Bank"


def test_tax_return_has_no_payer() -> None:
    assert _tax([("1040", None, False)]) == ("Alex/Tax/2024", "1040")


def test_tax_corrected_form() -> None:
    assert _tax([("w2", "Acme Corp", True)])[1] == "W-2 - Acme Corp (corrected)"
    assert file_name(_tax([("w2", "Acme Corp", True)])[1]) == "W-2 - Acme Corp (corrected).pdf"


def test_tax_several_forms_of_one_payer() -> None:
    forms = [("1099-div", "Fidelity", False), ("1099-int", "Fidelity", False)]
    assert _tax(forms)[1] == "1099-DIV, 1099-INT - Fidelity"


def test_tax_several_payers() -> None:
    forms = [("w2", "Acme", False), ("w2", "Globex", False)]
    assert _tax(forms)[1] == "W-2 - Multiple payers"


def test_tax_without_forms_or_year() -> None:
    folder, stem = tax_path(
        person=None, tax_year=None, forms=[], ingested_at="2025-01-05T00:00:00", sha256=SHA
    )
    assert folder == "Shared/Tax/Unknown year"
    assert stem == "Unrecognized - 2025-01-05 abababab"


def test_whole_paths_are_valid_on_every_platform() -> None:
    bad = re.compile(r'[<>:"\\|?*\x00-\x1f]')
    samples = [
        _statement(person="A:B", institution='Bank "X"', account_display_name='Bank "X" a|b?'),
        _tax([("w2", "Acme: <Corp>?", True)]),
    ]
    for folder, stem in samples:
        for part in [*folder.split("/"), file_name(stem)]:
            assert not bad.search(part), part
            assert part == part.strip() and not part.endswith(".")
            assert len(part) <= doc_names.MAX_COMPONENT_CHARS
