"""§6.6 performance budget: classify 300 distinct merchant keys in <= 6 LLM requests.

Opt-in only (`@pytest.mark.benchmark`; run with `uv run pytest -m benchmark tests/bench -v -s`).
The LLM is mocked (`classify/llm/batching.classify_all` calls `classify_batch`, monkeypatched here
to a deterministic, zero-latency fake, the same technique `tests/classify/test_batching.py`
uses) — no network call, and no dependency on the batch's actual content beyond its size, so this
is a pure test of `LLMConfig.batch_size` chunking (§3.7 "batching.py"): with the default
`batch_size=50`, 300 descriptions must chunk into exactly 6 calls, one per chunk, with no retries
(every row present, no `missing_ids`)."""

from __future__ import annotations

import time

import pytest

from personal_finance_analyzer.classify.llm import batching
from personal_finance_analyzer.classify.llm.egress import BatchResult
from personal_finance_analyzer.classify.llm.schema import Item
from personal_finance_analyzer.config import LLMConfig

pytestmark = pytest.mark.benchmark

_KEY_COUNT = 300
_REQUEST_BUDGET = 6


def _item(row_id: int) -> Item:
    return Item(
        id=row_id,
        category="grocery",
        subcategory="grocery_stores",
        merchant_canonical=f"Merchant {row_id}",
        confidence=0.9,
    )


def test_classify_300_distinct_keys_uses_at_most_6_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptions = [f"MERCHANT DISTINCT {i:04d}" for i in range(_KEY_COUNT)]
    calls: list[int] = []

    def _fake_classify_batch(batch_descriptions: list[str], cfg: LLMConfig) -> BatchResult:
        calls.append(len(batch_descriptions))
        items = tuple(_item(i) for i in range(1, len(batch_descriptions) + 1))
        return BatchResult(
            items=items,
            missing_ids=(),
            provider="anthropic",
            model="anthropic/claude-x",
            prompt_version="v1",
            schema_mode="json_schema",
            row_count=len(batch_descriptions),
            tokens_in=10,
            tokens_out=10,
            cost_usd=0.001,
            latency_ms=1,
            status="ok",
            request_sha256="deadbeef",
        )

    monkeypatch.setattr(batching, "classify_batch", _fake_classify_batch)

    cfg = LLMConfig(mode="remote", provider="anthropic", model="anthropic/claude-x")

    start = time.perf_counter()
    result = batching.classify_all(descriptions, cfg)
    elapsed = time.perf_counter() - start

    print(
        f"\n[bench] classify {_KEY_COUNT} distinct keys: {len(calls)} LLM request(s) "
        f"(budget {_REQUEST_BUDGET}), {elapsed * 1000:.1f} ms wall (mocked LLM)"
    )
    assert len(calls) <= _REQUEST_BUDGET, f"{len(calls)} LLM requests, budget is {_REQUEST_BUDGET}"
    assert all(needs_review is False for needs_review in (r.needs_review for r in result.results))
    assert len(result.results) == _KEY_COUNT
