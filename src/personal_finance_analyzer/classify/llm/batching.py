"""Batching, retries, and progress reporting for LLM classification (§3.7 "batching.py").

Splits a list of `description_clean` values into batches of `cfg.batch_size` (default 50; smaller
for local models with tight context), calls `egress.classify_batch` per batch, retries any row
that came back missing or invalid once in a smaller batch (size 10), and falls back rows that are
still missing to `others/uncategorized` with `needs_review=True` — **one bad row, or one failed
provider call, never fails a run** (matches the "no provider configured" first-class mode in
`egress.classify_batch`).

All batches issued for one user action share a `group_id` (uuid4, A2), so P2-B can persist one
`llm_runs` row per underlying call while reporting progress and cost per user action.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from personal_finance_analyzer.classify.llm.egress import BatchResult, classify_batch
from personal_finance_analyzer.classify.llm.schema import Item
from personal_finance_analyzer.config import LLMConfig
from personal_finance_analyzer.core.errors import LLMError

#: A batch that still has missing/invalid rows after the initial call is retried once, at this
#: (smaller) size, capped for local models with tight context (§3.7 "batching.py").
RETRY_BATCH_SIZE = 10

#: `progress_cb(done, total, cost_usd)` — called after every batch (including its retries), so a
#: caller (P2-C) can drive SSE without this module knowing anything about HTTP.
ProgressCallback = Callable[[int, int, float], None]


@dataclass(frozen=True, slots=True)
class ClassifyResult:
    """One `descriptions[i]` outcome. `item` always carries a valid category/subcategory pair
    (I4) — a row the LLM never classified successfully is filled with `others/uncategorized`,
    `confidence=0.0`, and `needs_review=True`."""

    description: str
    item: Item
    needs_review: bool
    #: True when `item` is the placeholder, not a model answer (so the row is "unplaced", not
    #: "classified by the LLM", and must not be cached).
    unplaced: bool = False


@dataclass(frozen=True, slots=True)
class ClassifyRunResult:
    """The outcome of one `classify_all` call: one `ClassifyResult` per input description, in
    the same order, plus every underlying LLM call's `BatchResult` for P2-B to persist as
    `llm_runs` rows (this module writes nothing to the database itself)."""

    group_id: str
    results: tuple[ClassifyResult, ...]
    runs: tuple[BatchResult, ...]


def _fallback_item(row_id: int) -> Item:
    """The `others/uncategorized`, `needs_review` placeholder for a row the LLM never
    successfully classified (initial call failed, or the row was still missing after retry)."""
    return Item(
        id=row_id,
        category="others",
        subcategory="uncategorized",
        merchant_canonical="",
        confidence=0.0,
        is_online_store=False,
    )


def _chunk(seq: list[str], size: int) -> list[list[str]]:
    step = max(1, size)
    return [seq[i : i + step] for i in range(0, len(seq), step)]


def classify_all(
    descriptions: list[str],
    cfg: LLMConfig,
    *,
    group_id: str | None = None,
    progress_cb: ProgressCallback | None = None,
) -> ClassifyRunResult:
    """Classify every entry of `descriptions`, batching, retrying, and falling back as needed.

    Args:
        descriptions: one `description_clean` per row (already normalized and safe to egress).
        cfg: the LLM provider configuration; `cfg.batch_size` sets the batch size.
        group_id: shared across every batch of one user action (A2); a `uuid4` is generated when
            omitted.
        progress_cb: called as `progress_cb(done, total, cost_usd)` after every batch (and its
            retries), with `cost_usd` the running total across the whole call.

    Returns:
        A `ClassifyRunResult` with one `ClassifyResult` per input description, in input order,
        and every `BatchResult` issued (initial calls and retries alike).
    """
    resolved_group_id = group_id or str(uuid4())
    total = len(descriptions)
    done = 0
    runs: list[BatchResult] = []
    # Filled in as each chunk (and retry) resolves; every index 0..total-1 ends up here.
    resolved: dict[int, Item] = {}
    needs_review: set[int] = set()

    offset = 0
    for chunk in _chunk(descriptions, cfg.batch_size):
        chunk_indices = list(range(offset, offset + len(chunk)))
        offset += len(chunk)

        try:
            result = classify_batch(chunk, cfg)
        except LLMError:
            _fallback_all(chunk_indices, resolved, needs_review)
        else:
            runs.append(result)
            for item in result.items:
                resolved[chunk_indices[item.id - 1]] = item
            if result.missing_ids:
                _retry_missing(
                    chunk=chunk,
                    chunk_indices=chunk_indices,
                    missing_ids=result.missing_ids,
                    cfg=cfg,
                    resolved=resolved,
                    needs_review=needs_review,
                    runs=runs,
                )

        done += len(chunk)
        if progress_cb is not None:
            progress_cb(done, total, sum(r.cost_usd for r in runs))

    results = tuple(
        ClassifyResult(
            description=descriptions[i],
            item=resolved[i],
            needs_review=i in needs_review,
            unplaced=i in needs_review,
        )
        for i in range(total)
    )
    return ClassifyRunResult(group_id=resolved_group_id, results=results, runs=tuple(runs))


def _fallback_all(
    chunk_indices: list[int], resolved: dict[int, Item], needs_review: set[int]
) -> None:
    """Fill every index in `chunk_indices` with the fallback item (the whole call failed)."""
    for local_id, global_idx in enumerate(chunk_indices, start=1):
        resolved[global_idx] = _fallback_item(local_id)
        needs_review.add(global_idx)


def _retry_missing(
    *,
    chunk: list[str],
    chunk_indices: list[int],
    missing_ids: tuple[int, ...],
    cfg: LLMConfig,
    resolved: dict[int, Item],
    needs_review: set[int],
    runs: list[BatchResult],
) -> None:
    """Retry every row named in `missing_ids` exactly once, in sub-batches of
    `RETRY_BATCH_SIZE`. A row still missing (or invalid) after its retry is filled with the
    `others/uncategorized` fallback and marked `needs_review` — one bad row never fails a run."""
    missing_local_ids = list(missing_ids)
    for start in range(0, len(missing_local_ids), RETRY_BATCH_SIZE):
        retry_local_ids = missing_local_ids[start : start + RETRY_BATCH_SIZE]
        retry_descriptions = [chunk[local_id - 1] for local_id in retry_local_ids]
        # Within this retry sub-batch, `classify_batch` assigns its own 1..len ids; map them
        # back to the original chunk-local ids via `retry_local_ids`.
        retry_chunk_indices = [chunk_indices[local_id - 1] for local_id in retry_local_ids]

        try:
            retry_result = classify_batch(retry_descriptions, cfg)
        except LLMError:
            _fallback_all(retry_chunk_indices, resolved, needs_review)
            continue

        runs.append(retry_result)
        for item in retry_result.items:
            resolved[retry_chunk_indices[item.id - 1]] = item
        for missing_retry_id in retry_result.missing_ids:
            global_idx = retry_chunk_indices[missing_retry_id - 1]
            resolved[global_idx] = _fallback_item(missing_retry_id)
            needs_review.add(global_idx)
