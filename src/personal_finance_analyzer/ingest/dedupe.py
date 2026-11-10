"""Dedupe contract (§3.5, A6).

`dedupe_hash` distinguishes genuinely identical same-day charges while keeping an overlapping
reimport idempotent. The `occurrence_index` it embeds is computed **within the incoming
statement only** — never offset by what already exists in the database (see the module-level
warning in §3.5): grouping by ``(posted_date, amount_minor, description_raw)`` and assigning
``0, 1, 2, ...`` in statement order within each group. `ingest/pipeline.py` is the only caller;
this module has no database dependency of its own so the hashing rule can be unit-tested in
isolation from persistence.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DedupeKey:
    """The three raw fields §3.5 groups same-day duplicates by, plus the account they belong to."""

    account_id: int
    posted_date: str  # ISO-8601, as stored in `transactions.posted_date`
    amount_minor: int
    description_raw: str


def compute_occurrence_indices(keys: Sequence[DedupeKey]) -> tuple[int, ...]:
    """Assign each of ``keys`` its `occurrence_index` (§3.5).

    Rows sharing an identical ``(account_id, posted_date, amount_minor, description_raw)`` tuple
    are a group; within a group, indices are assigned ``0, 1, 2, ...`` in the order ``keys``
    already carries — which must be the statement's own row order, never resorted here, since
    "statement order" is exactly what makes an overlapping reimport line up with the rows already
    stored (§3.5).

    Returns:
        One index per element of ``keys``, aligned by position.
    """
    counters: dict[tuple[int, str, int, str], int] = {}
    indices: list[int] = []
    for key in keys:
        group = (key.account_id, key.posted_date, key.amount_minor, key.description_raw)
        index = counters.get(group, 0)
        counters[group] = index + 1
        indices.append(index)
    return tuple(indices)


def compute_dedupe_hash(key: DedupeKey, occurrence_index: int) -> str:
    """Return the `dedupe_hash` (§3.5) for one row: a SHA-256 hex digest of
    ``f"{account_id}|{posted_date}|{amount_minor}|{description_raw}|{occurrence_index}"``.

    Args:
        key: the row's account id, posted date (ISO-8601), signed `amount_minor`, and
            `description_raw`.
        occurrence_index: this row's position within its `(account_id, posted_date, amount_minor,
            description_raw)` group, from `compute_occurrence_indices`.
    """
    payload = (
        f"{key.account_id}|{key.posted_date}|{key.amount_minor}|"
        f"{key.description_raw}|{occurrence_index}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
