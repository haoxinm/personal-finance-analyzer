"""Tests for `classify/llm/batching.py` (§3.7 "batching.py")."""

from __future__ import annotations

from typing import Any

import pytest

from personal_finance_analyzer.classify.llm import batching
from personal_finance_analyzer.classify.llm.egress import BatchResult
from personal_finance_analyzer.classify.llm.schema import Item
from personal_finance_analyzer.config import LLMConfig


def _cfg(batch_size: int = 50) -> LLMConfig:
    return LLMConfig(
        mode="remote", provider="anthropic", model="anthropic/claude-x", batch_size=batch_size
    )


def _item(row_id: int, *, category: str = "grocery", subcategory: str = "grocery_stores") -> Item:
    return Item(
        id=row_id,
        category=category,  # type: ignore[arg-type]
        subcategory=subcategory,
        merchant_canonical=f"Merchant {row_id}",
        confidence=0.9,
    )


def _ok_result(descriptions: list[str], *, missing_ids: tuple[int, ...] = ()) -> BatchResult:
    items = tuple(_item(i) for i in range(1, len(descriptions) + 1) if i not in missing_ids)
    return BatchResult(
        items=items,
        missing_ids=missing_ids,
        provider="anthropic",
        model="anthropic/claude-x",
        prompt_version="v1",
        schema_mode="json_schema",
        row_count=len(descriptions),
        tokens_in=10,
        tokens_out=10,
        cost_usd=0.01,
        latency_ms=5,
        status="ok" if not missing_ids else "partial",
        request_sha256="deadbeef",
    )


def test_classify_all_batches_120_descriptions_into_exactly_3_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _fake_classify_batch(descriptions: list[str], cfg: LLMConfig) -> BatchResult:
        calls.append(list(descriptions))
        return _ok_result(descriptions)

    monkeypatch.setattr(batching, "classify_batch", _fake_classify_batch)

    descriptions = [f"MERCHANT {i}" for i in range(120)]
    run = batching.classify_all(descriptions, _cfg(batch_size=50))

    assert len(calls) == 3
    assert [len(c) for c in calls] == [50, 50, 20]
    assert len(run.results) == 120
    assert all(not r.needs_review for r in run.results)
    assert run.results[0].description == "MERCHANT 0"


def test_classify_all_retries_missing_ids_once_in_smaller_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _fake_classify_batch(descriptions: list[str], cfg: LLMConfig) -> BatchResult:
        calls.append(list(descriptions))
        if len(calls) == 1:
            # First (initial) call: 50 rows, 2 missing (ids 10 and 40).
            return _ok_result(descriptions, missing_ids=(10, 40))
        # Retry call: both missing rows resolve successfully.
        return _ok_result(descriptions)

    monkeypatch.setattr(batching, "classify_batch", _fake_classify_batch)

    descriptions = [f"MERCHANT {i}" for i in range(50)]
    run = batching.classify_all(descriptions, _cfg(batch_size=50))

    assert len(calls) == 2  # exactly one retry
    assert len(calls[1]) == 2  # retry batch size <= 10, here exactly the 2 missing rows
    assert calls[1] == ["MERCHANT 9", "MERCHANT 39"]  # 1-based ids 10 and 40 -> 0-based 9, 39
    assert all(not r.needs_review for r in run.results)
    assert len(run.runs) == 2


def test_classify_all_marks_still_missing_rows_needs_review_after_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _fake_classify_batch(descriptions: list[str], cfg: LLMConfig) -> BatchResult:
        calls.append(list(descriptions))
        if len(calls) == 1:
            return _ok_result(descriptions, missing_ids=(1, 2))
        # Retry: still missing the second row (local id 2 within the retry batch).
        return _ok_result(descriptions, missing_ids=(2,))

    monkeypatch.setattr(batching, "classify_batch", _fake_classify_batch)

    descriptions = ["A", "B", "C"]
    run = batching.classify_all(descriptions, _cfg(batch_size=50))

    assert len(calls) == 2
    assert run.results[0].needs_review is False  # first missing id was recovered on retry
    assert run.results[1].needs_review is True  # still missing after retry -> fallback
    assert run.results[1].item.category == "others"
    assert run.results[1].item.subcategory == "uncategorized"
    assert run.results[1].item.confidence == 0.0
    assert [r.unplaced for r in run.results] == [False, True, False]
    assert run.results[2].needs_review is False  # untouched row, classified in the initial call


def test_classify_all_splits_more_than_10_missing_ids_into_retry_sub_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _fake_classify_batch(descriptions: list[str], cfg: LLMConfig) -> BatchResult:
        calls.append(list(descriptions))
        if len(calls) == 1:
            missing = tuple(range(1, 16))  # 15 missing ids out of 20
            return _ok_result(descriptions, missing_ids=missing)
        return _ok_result(descriptions)

    monkeypatch.setattr(batching, "classify_batch", _fake_classify_batch)

    descriptions = [f"M{i}" for i in range(20)]
    batching.classify_all(descriptions, _cfg(batch_size=50))

    assert len(calls) == 3  # initial + 2 retry sub-batches (10 + 5, capped at RETRY_BATCH_SIZE)
    assert len(calls[1]) == 10
    assert len(calls[2]) == 5


def test_classify_all_no_provider_configured_falls_back_every_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A real classify_batch call raises LLMError("no provider configured") for mode="none";
    # exercise the same behavior at the batching layer with an LLMError-raising stub.
    from personal_finance_analyzer.core.errors import LLMError

    def _raise(descriptions: list[str], cfg: LLMConfig) -> BatchResult:
        raise LLMError("no provider configured")

    monkeypatch.setattr(batching, "classify_batch", _raise)

    descriptions = ["A", "B"]
    run = batching.classify_all(descriptions, _cfg())

    assert all(r.needs_review for r in run.results)
    assert all(r.item.category == "others" for r in run.results)
    assert run.runs == ()  # no successful call to record


def test_classify_all_provider_failure_never_raises_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from personal_finance_analyzer.core.errors import LLMError

    def _raise(descriptions: list[str], cfg: LLMConfig) -> BatchResult:
        raise LLMError("network timeout")

    monkeypatch.setattr(batching, "classify_batch", _raise)

    run = batching.classify_all(["A"], _cfg())  # must not raise
    assert run.results[0].needs_review is True


def test_classify_all_progress_callback_reports_done_total_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_classify_batch(descriptions: list[str], cfg: LLMConfig) -> BatchResult:
        return _ok_result(descriptions)

    monkeypatch.setattr(batching, "classify_batch", _fake_classify_batch)

    progress_calls: list[tuple[int, int, float]] = []

    def _progress_cb(done: int, total: int, cost: float) -> None:
        progress_calls.append((done, total, cost))

    descriptions = [f"M{i}" for i in range(75)]
    batching.classify_all(descriptions, _cfg(batch_size=50), progress_cb=_progress_cb)

    assert progress_calls == [(50, 75, 0.01), (75, 75, 0.02)]


def test_classify_all_shares_one_group_id_across_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_classify_batch(descriptions: list[str], cfg: LLMConfig) -> BatchResult:
        return _ok_result(descriptions)

    monkeypatch.setattr(batching, "classify_batch", _fake_classify_batch)

    run1 = batching.classify_all(["A"], _cfg())
    run2 = batching.classify_all(["A"], _cfg())
    assert run1.group_id != run2.group_id  # a fresh uuid4 per call when not supplied

    run3 = batching.classify_all(["A", "B"], _cfg(batch_size=1), group_id="fixed-group")
    assert run3.group_id == "fixed-group"
    assert len(run3.runs) == 2  # two batches, one shared group_id


def test_classify_all_empty_input_returns_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*args: Any, **kwargs: Any) -> BatchResult:
        raise AssertionError("classify_batch must not be called for empty input")

    monkeypatch.setattr(batching, "classify_batch", _fail)

    run = batching.classify_all([], _cfg())
    assert run.results == ()
    assert run.runs == ()
