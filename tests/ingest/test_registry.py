"""Tests for the parser registry (P1-B)."""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

import pytest

from personal_finance_analyzer.core.types import (
    ExtractedDoc,
    PageText,
    ParsedStatement,
    StatementParser,
)
from personal_finance_analyzer.ingest import registry
from personal_finance_analyzer.ingest.parsers.generic_table import generic_table

_EMPTY_DOC = ExtractedDoc(
    file_sha256="0" * 64,
    page_count=1,
    pages=(PageText(page_number=1, text="", words=(), char_count=0),),
    has_text_layer=False,
)


class _FixedScoreParser:
    """A minimal `StatementParser` stand-in with a fixed `detect()` score, for registry tests
    that must not depend on any real layout parser being present."""

    def __init__(self, parser_id: str, score: float, *, raises: bool = False) -> None:
        self.id = parser_id
        self.version = "1.0.0"
        self.account_type = "credit"
        self._score = score
        self._raises = raises

    def detect(self, doc: ExtractedDoc) -> float:
        if self._raises:
            raise RuntimeError("boom")
        return self._score

    def parse(self, doc: ExtractedDoc) -> ParsedStatement:  # pragma: no cover - not exercised
        raise NotImplementedError


def _as_parser(parser: _FixedScoreParser) -> StatementParser:
    """Narrow to `StatementParser` for `select()`'s signature; `_FixedScoreParser` implements
    the protocol structurally (checked by `isinstance` in the registry itself)."""
    assert isinstance(parser, StatementParser)
    return parser


def test_select_prefers_the_highest_scoring_parser_above_threshold() -> None:
    low = _FixedScoreParser("layout_low", 0.4)
    high = _FixedScoreParser("layout_high", 0.9)
    selected = registry.select(_EMPTY_DOC, parsers=(_as_parser(low), _as_parser(high)))
    assert selected is _as_parser(high)


def test_select_falls_back_to_generic_when_nothing_scores_above_threshold() -> None:
    low = _FixedScoreParser("layout_low", 0.5)  # exactly at the threshold: not "above" it
    selected = registry.select(_EMPTY_DOC, parsers=(_as_parser(low),))
    assert selected is generic_table


def test_select_falls_back_to_generic_with_no_candidates() -> None:
    assert registry.select(_EMPTY_DOC, parsers=()) is generic_table


def test_select_treats_a_raising_detect_as_score_zero() -> None:
    raiser = _FixedScoreParser("layout_raiser", 0.0, raises=True)
    good = _FixedScoreParser("layout_good", 0.6)
    selected = registry.select(_EMPTY_DOC, parsers=(_as_parser(raiser), _as_parser(good)))
    assert selected is _as_parser(good)

    # And with only the raising parser present, the registry still falls back cleanly.
    assert registry.select(_EMPTY_DOC, parsers=(_as_parser(raiser),)) is generic_table


def test_generic_fallback_is_never_in_the_scored_candidate_pool() -> None:
    """§2d.2/D3: the generic parser is the guaranteed default, never a competitor. Even a
    document that scores it highly must not select it *as a scored candidate* — `select()`
    reaches it only via the fallback branch."""
    for parser in registry.discover_all_parsers():
        assert parser.id != generic_table.id


def test_discover_builtin_parsers_excludes_the_generic_module() -> None:
    builtin_ids = {parser.id for parser in registry.discover_builtin_parsers()}
    assert generic_table.id not in builtin_ids


def test_discover_entrypoint_parsers_returns_a_tuple_without_raising() -> None:
    # No third-party parsers are registered under the entry-point group in this repository's
    # own `pyproject.toml`; this simply proves discovery never raises when the group is empty.
    assert registry.discover_entrypoint_parsers() == ()


def test_select_with_default_candidates_never_raises_and_returns_a_parser() -> None:
    selected = registry.select(_EMPTY_DOC)
    assert selected is not None
    assert hasattr(selected, "id") and hasattr(selected, "detect") and hasattr(selected, "parse")


# --------------------------------------------------------------------------------------------
# Robustness of discovery itself: a broken built-in module or a broken/non-conforming
# third-party entry point must be skipped with a warning, never crash discovery for the rest.
# --------------------------------------------------------------------------------------------


class _FakeModuleInfo:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEntryPoint:
    def __init__(self, name: str, value: str, loaded: object) -> None:
        self.name = name
        self.value = value
        self._loaded = loaded

    def load(self) -> object:
        if isinstance(self._loaded, Exception):
            raise self._loaded
        return self._loaded


def test_discover_builtin_parsers_skips_a_module_that_fails_to_import(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import personal_finance_analyzer.ingest.parsers as parsers_pkg

    def fake_iter_modules(path: object, prefix: str) -> list[_FakeModuleInfo]:
        return [_FakeModuleInfo(f"{parsers_pkg.__name__}.layout_broken")]

    def fake_import_module(name: str) -> ModuleType:
        raise ImportError("simulated broken parser module")

    monkeypatch.setattr(pkgutil, "iter_modules", fake_iter_modules)
    monkeypatch.setattr(importlib, "import_module", fake_import_module)

    with caplog.at_level("WARNING"):
        found = registry.discover_builtin_parsers()

    assert found == ()
    assert any("layout_broken" in message for message in caplog.messages)


def test_discover_entrypoint_parsers_skips_broken_and_non_conforming_entry_points(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    good = _FixedScoreParser("layout_from_plugin", 0.7)
    entry_points_result = [
        _FakeEntryPoint("broken", "some.module:broken", RuntimeError("boom")),
        _FakeEntryPoint("not_a_parser", "some.module:not_a_parser", object()),
        _FakeEntryPoint("good", "some.module:good", good),
    ]

    def fake_entry_points(*, group: str) -> list[_FakeEntryPoint]:
        assert group == registry.ENTRY_POINT_GROUP
        return entry_points_result

    monkeypatch.setattr(registry, "entry_points", fake_entry_points)

    with caplog.at_level("WARNING"):
        found = registry.discover_entrypoint_parsers()

    assert found == (good,)
    assert any("broken" in message for message in caplog.messages)
    assert any("not_a_parser" in message for message in caplog.messages)
