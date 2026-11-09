"""Which person and which kind of document a path means (`watch.classify`): pure rules."""

from __future__ import annotations

import unicodedata
from pathlib import PurePosixPath

import pytest

from personal_finance_analyzer.watch.classify import PATH_CODES, PathClass, classify_path

PEOPLE = [(1, "Alex"), (2, "Sam Lee"), (3, "José")]


def classify(path: str, people: list[tuple[int, str]] | None = None) -> PathClass:
    return classify_path(PurePosixPath(path).parts, PEOPLE if people is None else people)


def test_a_statement_under_a_person_and_statements() -> None:
    cls = classify("Alex/Statements/Chase/2026-01 Chase credit 1234.pdf")
    assert (cls.status, cls.kind, cls.user_id, cls.person_name) == (
        "ok",
        "statement",
        1,
        "Alex",
    )
    assert cls.code is None


@pytest.mark.parametrize(
    "path",
    [
        "Alex/Statements/x.pdf",
        "Alex/Statements/Chase/x.pdf",
        "Alex/Statements/Chase/Checking/2026/Q1/x.pdf",
        "Alex/Statements/Anything I Named/x.pdf",
    ],
)
def test_statements_may_sit_at_any_depth_under_any_folder(path: str) -> None:
    cls = classify(path)
    assert cls.status == "ok" and cls.kind == "statement" and cls.user_id == 1


def test_tax_forms_carry_the_folder_year() -> None:
    cls = classify("Alex/Tax/2024/W-2 - Acme.pdf")
    assert (cls.status, cls.kind, cls.folder_year) == ("ok", "tax", 2024)


def test_tax_year_folder_is_found_with_deeper_subfolders_and_ignored_when_not_a_year() -> None:
    assert classify("Alex/Tax/2023/Employers/Acme/w2.pdf").folder_year == 2023
    assert classify("Alex/Tax/w2.pdf").folder_year is None
    assert classify("Alex/Tax/Unknown year/w2.pdf").folder_year is None
    assert classify("Alex/Tax/1850/w2.pdf").folder_year is None
    assert classify("Alex/Statements/2024/x.pdf").folder_year is None


def test_person_matching_ignores_case_and_whitespace() -> None:
    assert classify("ALEX/Statements/x.pdf").user_id == 1
    assert classify("sam   lee/STATEMENTS/x.pdf").user_id == 2
    assert classify("Sam Lee /tax/2024/x.pdf").user_id == 2


def test_person_matching_ignores_unicode_form() -> None:
    nfd = unicodedata.normalize("NFD", "José")
    nfc = unicodedata.normalize("NFC", "José")
    assert nfd != nfc
    assert classify(f"{nfd}/Statements/x.pdf").user_id == 3
    assert classify(f"{nfc}/Statements/x.pdf", [(3, nfd)]).user_id == 3


def test_the_apps_own_folder_name_always_matches() -> None:
    # `A/B` is filed under `A-B`; the folder the app made must find its person again.
    cls = classify("A-B/Statements/x.pdf", [(7, "A/B")])
    assert cls.status == "ok" and cls.user_id == 7
    assert classify("Alex 12345/Statements/x.pdf", [(7, "Alex 12345")]).user_id == 7


def test_an_unknown_person_is_reported_with_a_hint_and_never_created() -> None:
    cls = classify("Taylor/Statements/x.pdf")
    assert cls.status == "unknown_person" and cls.user_id is None
    assert cls.code == "unknown_person" and cls.code in PATH_CODES
    assert '"Taylor"' in cls.message and "Taylor" in cls.hint


def test_two_people_with_one_folder_name_are_ambiguous() -> None:
    cls = classify("sam/Statements/x.pdf", [(1, "Sam"), (2, "SAM")])
    assert cls.status == "ambiguous_person" and cls.code == "ambiguous_person"


def test_shared_needs_a_person() -> None:
    cls = classify("Shared/Statements/x.pdf")
    assert cls.status == "shared" and cls.code == "shared_needs_person"
    assert cls.kind == "statement"


def test_a_person_actually_named_shared_wins() -> None:
    assert classify("Shared/Statements/x.pdf", [(9, "Shared")]).user_id == 9


@pytest.mark.parametrize(
    "path",
    [
        "loose.pdf",
        "Alex/loose.pdf",
        "Alex/Receipts/x.pdf",
        "Alex/Misc/2024/x.pdf",
    ],
)
def test_anything_elsewhere_is_not_in_a_recognised_folder(path: str) -> None:
    cls = classify(path)
    assert cls.status == "unrecognised" and cls.code == "not_recognised_folder"
    assert "<Person>/Statements/" in cls.hint
