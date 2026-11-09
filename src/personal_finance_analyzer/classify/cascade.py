"""The classification cascade (§3.12a, §2b P2-B, A28).

`classify_transactions` is the only entry point that reaches the LLM. Every other function here
is a deterministic, local, first-hit-wins cascade over already-persisted `Transaction` rows.

There are two kinds of user correction, and they never mix:

- A **one-time edit** (a "patch": the edit popover, a bulk update, Review's Accept) changes exactly
  one transaction and marks it `classified_by='user'`. It writes no rule and no `merchant_map`
  row, so it says nothing about any other or future transaction of the merchant.
- A **merchant rule** (Review's "Always categorize ... this way", the edit popover's "Also create a
  rule", or a rule added on the Rules screen) is a `rules` row with `source='user'`. It decides
  future transactions of the merchant, and existing ones only through an explicit "Apply to
  existing".

Precedence, highest first: one-time edit > your rules > built-in rules > merchant cache > LLM.

0. **One-time edits** — a transaction with `classified_by='user'` is never processed by anything
   here: not by a re-run, not by a rule, not by the cache or the LLM (I6). The cascade skips it
   outright, so no step below can rewrite it.
1. **Kind** — `Transaction.kind` is set by the ingest pipeline (P2-A) from `classify/kinds.py`,
   *before* this module ever sees the row. If it is already `payment` or `transfer`, this module
   assigns `others/payments_transfers`, `is_spend=0`, `classified_by='kind'`, and stops: these
   transactions never reach the LLM (I11, A26).
2. **User rules** — `rules` where `source='user'`, by `priority` then `id`, matched against
   `merchant_key` (`classify/rules.py`).
3. **Legacy merchant pins** — a `merchant_map` hit with `source='user'`. Older versions wrote one
   silently on every single-transaction edit; nothing writes them any more, and the data
   migration `0010_user_merchant_pins_to_rules` turns the ones that exist into visible exact
   rules (priority 1000, i.e. after every ordinary rule). A pin that survives anyway (a database
   not yet migrated) is still honored, but ranks *below* your rules, exactly like that low-priority
   rule would, and is recorded as `classified_by='user_rule'` (never as a one-time edit, so it can
   still be changed later). Increments `hit_count`.
4. **Built-in rules** — `rules` where `source='builtin'`, seeded from `builtin_rules.yaml` by
   `rules.sync_builtin_rules` (D8).
5. **Issuer category** — `Transaction.issuer_category` resolved via `rules.resolve_issuer_category`
   (A16); writes `merchant_map(source='issuer')` on a hit. `issuer_category` is local-only (I1b)
   and never appears in any egress payload.
6. **Learned cache** — a `merchant_map` hit with `source IN ('llm', 'issuer')`; increments
   `hit_count`.
7. **LLM batch** — everything left, deduplicated by `merchant_key`, one representative
   `description_clean` per key (A5: the modal value, tie-broken shortest then lexicographic).
   A row the LLM never successfully classifies (`classify.llm.batching`'s own fallback, or no
   provider configured at all, D2) lands in `others/uncategorized` with `needs_review=True` —
   step 8's fallback is already built into that result, so there is no separate step here.

A one-time edit (`classified_by='user'`) sits above all of these and is final for every pass of
`classify_transactions`. The one way out is `revert_to_automatic`: it clears the edit and runs steps
1-6 (never the LLM) for that row.

"Always categorize this merchant this way" (`create_rule`) updates the merchant's own exact rule
instead of stacking a second one, so the newest choice wins; it also patches the transaction it
was chosen on.

`merchant_key` is never written to `merchant_map` when empty (A27). A result's
`confidence < cfg.confidence_threshold` always sets `needs_review=True`, on top of any
step-specific reason (a dynamic online-shopping subcategory still pending approval, most notably).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.classify.kinds import is_spend as kind_is_spend
from personal_finance_analyzer.classify.llm.batching import classify_all
from personal_finance_analyzer.classify.llm.egress import BatchResult, preview_payload
from personal_finance_analyzer.classify.llm.schema import Item
from personal_finance_analyzer.classify.rules import (
    IssuerCategoryMatch,
    resolve_issuer_category,
    rule_matches,
    sync_builtin_rules,
)
from personal_finance_analyzer.classify.taxonomy import is_dynamic
from personal_finance_analyzer.config import LLMConfig
from personal_finance_analyzer.core.errors import ConfigError
from personal_finance_analyzer.core.types import CLASSIFIED_BY_VALUES, USER_CLASSIFIED, Kind
from personal_finance_analyzer.db.models import (
    Category,
    Classification,
    LlmRun,
    MerchantMap,
    Rule,
    Subcategory,
    Transaction,
    utcnow_iso,
)

logger = logging.getLogger(__name__)

#: Kinds that never reach the classification cascade at all (I11, A26, cascade step 1).
_NON_SPEND_KINDS: frozenset[str] = frozenset({"payment", "transfer"})

#: How many transactions the deterministic phase (steps 1-6) processes before an intermediate
#: commit, so a long run's progress survives a crash or reload without waiting for the whole
#: batch (the plan's "commits per batch"; the LLM phase commits once, after `classify_all`
#: returns, since it already batches and retries internally — see `classify_transactions`).
_DETERMINISTIC_COMMIT_CHUNK = 200

#: `others/payments_transfers` — the fixed destination for every payment/transfer row (I11).
_PAYMENTS_TRANSFERS_CATEGORY = "others"
_PAYMENTS_TRANSFERS_SUBCATEGORY = "payments_transfers"
_UNCATEGORIZED_CATEGORY = "others"
_UNCATEGORIZED_SUBCATEGORY = "uncategorized"


@dataclass(frozen=True, slots=True)
class ClassifyResult:
    """The outcome of one `classify_transactions` call (§3.12a)."""

    group_id: str
    classified: int
    needs_review: int
    llm_requests: int
    cost_usd: float


@dataclass(frozen=True, slots=True)
class _Outcome:
    """One cascade step's proposed classification for a transaction, before it is written."""

    category_key: str
    subcategory_key: str
    confidence: float | None
    classified_by: str  # cache|user_rule|builtin_rule|issuer|llm|user (one-time edit)|kind
    merchant_canonical: str | None = None
    kind_override: str | None = None
    needs_review_override: bool = False


def classify_transactions(
    session_factory: Callable[[], Session],
    transaction_ids: Sequence[int],
    *,
    cfg: LLMConfig,
    group_id: str,
    progress_cb: Callable[[int, int, float], None],
) -> ClassifyResult:
    """Run the cascade over `transaction_ids` and persist a classification for every one of them.

    Args:
        session_factory: opens a new `Session`; called once per phase (the deterministic
            steps 1-6, then, if anything remains, the LLM phase), so a caller-provided
            connection pool is never held open longer than necessary.
        transaction_ids: the transactions to classify. An id that no longer exists is skipped.
        cfg: the LLM provider configuration (`[llm]`, `config.toml`).
        group_id: shared across every `llm_runs` row this call writes (A2).
        progress_cb: called as `progress_cb(done, total, cost_usd)` after every deterministic
            commit chunk and once more after the LLM phase completes. `done`/`total` count
            transactions; during the LLM phase, `done` is estimated from `classify_all`'s own
            (merchant-key-counted) progress, since a merchant key can cover several transactions.

    Returns:
        A `ClassifyResult` summarizing the run. `classified` counts every transaction processed
        (every one ends up with a category and subcategory, I4 — including the
        `others/uncategorized` fallback); `needs_review` counts those flagged for review;
        `llm_requests` is the number of underlying LLM calls actually made (initial calls and
        retries); `cost_usd` is their summed cost.
    """
    total = len(transaction_ids)
    if total == 0:
        progress_cb(0, 0, 0.0)
        return ClassifyResult(
            group_id=group_id, classified=0, needs_review=0, llm_requests=0, cost_usd=0.0
        )

    classified = 0
    needs_review = 0
    pending_llm: dict[str, list[int]] = {}

    session = session_factory()
    try:
        sync_builtin_rules(session)
        session.commit()

        user_rules, builtin_rules = _load_rule_sets(session)

        done = 0
        since_commit = 0
        for txn_id in transaction_ids:
            txn = session.get(Transaction, txn_id)
            done += 1
            since_commit += 1
            if txn is None or txn.classified_by == USER_CLASSIFIED:
                # A one-time edit is final: nothing in the cascade may reconsider it.
                continue
            outcome = _classify_deterministic(
                session, txn, user_rules=user_rules, builtin_rules=builtin_rules
            )
            if outcome is None:
                pending_llm.setdefault(txn.merchant_key, []).append(txn.id)
            else:
                _apply_outcome(session, txn, outcome, cfg=cfg)
                classified += 1
                if txn.needs_review:
                    needs_review += 1
            if since_commit >= _DETERMINISTIC_COMMIT_CHUNK:
                session.commit()
                since_commit = 0
                progress_cb(done, total, 0.0)
        session.commit()
        progress_cb(done, total, 0.0)
    finally:
        session.close()

    llm_requests = 0
    cost_usd = 0.0
    if pending_llm:
        classified_delta, needs_review_delta, llm_requests, cost_usd = _run_llm_phase(
            session_factory,
            pending_llm,
            cfg=cfg,
            group_id=group_id,
            deterministic_done=done,
            total=total,
            progress_cb=progress_cb,
        )
        classified += classified_delta
        needs_review += needs_review_delta

    return ClassifyResult(
        group_id=group_id,
        classified=classified,
        needs_review=needs_review,
        llm_requests=llm_requests,
        cost_usd=cost_usd,
    )


def _run_llm_phase(
    session_factory: Callable[[], Session],
    pending_llm: dict[str, list[int]],
    *,
    cfg: LLMConfig,
    group_id: str,
    deterministic_done: int,
    total: int,
    progress_cb: Callable[[int, int, float], None],
) -> tuple[int, int, int, float]:
    """Classify every merchant key in `pending_llm` via the LLM and persist the results.

    Returns `(classified, needs_review, llm_requests, cost_usd)` for this phase only.
    """
    merchant_keys = sorted(pending_llm)

    session = session_factory()
    try:
        representative = {
            key: _select_representative(
                [
                    d
                    for d in (_description_of(session, tid) for tid in pending_llm[key])
                    if d is not None
                ]
            )
            for key in merchant_keys
        }

        def _llm_progress(llm_done: int, llm_total: int, running_cost: float) -> None:
            if llm_total <= 0:
                return
            fraction = llm_done / llm_total
            done = deterministic_done + round(fraction * (total - deterministic_done))
            progress_cb(min(total, done), total, running_cost)

        run_result = classify_all(
            [representative[key] for key in merchant_keys],
            cfg,
            group_id=group_id,
            progress_cb=_llm_progress,
        )

        llm_run_ids = [_persist_llm_run(session, run, group_id=group_id) for run in run_result.runs]
        # `classify_all` does not expose which underlying batch (initial call vs. retry)
        # resolved each result, so every `classifications` row from this call is linked to the
        # *first* run — every `llm_runs` row this call produced still shares `group_id`, which
        # is what auditing (`GET /api/classify/runs`) groups by.
        primary_llm_run_id = llm_run_ids[0] if llm_run_ids else None

        classified = 0
        needs_review = 0
        for key, result in zip(merchant_keys, run_result.results, strict=True):
            outcome = _outcome_from_item(session, result.item, batch_flagged=result.needs_review)
            _write_merchant_map(session, key, outcome, source="llm")
            for txn_id in pending_llm[key]:
                txn = session.get(Transaction, txn_id)
                if txn is None or txn.classified_by == USER_CLASSIFIED:
                    # Edited by hand while the model was running: the edit stands.
                    continue
                _apply_outcome(session, txn, outcome, cfg=cfg, llm_run_id=primary_llm_run_id)
                classified += 1
                if txn.needs_review:
                    needs_review += 1
        session.commit()
        progress_cb(total, total, sum(r.cost_usd for r in run_result.runs))
        return (
            classified,
            needs_review,
            len(run_result.runs),
            sum(r.cost_usd for r in run_result.runs),
        )
    finally:
        session.close()


def preview_egress(session: Session, transaction_ids: Sequence[int]) -> str:
    """Return exactly the CSV `classify_transactions` would send next for `transaction_ids`.

    Applies cascade steps 1-6 as a dry run (no writes, including no `merchant_map` writes) to
    determine which transactions would actually reach the LLM, then deduplicates the rest by
    `merchant_key` exactly as step 7 does (A5). Makes no network call.
    """
    sync_builtin_rules(session)
    user_rules, builtin_rules = _load_rule_sets(session)

    pending: dict[str, list[str]] = {}
    for txn_id in transaction_ids:
        txn = session.get(Transaction, txn_id)
        if txn is None or txn.classified_by == USER_CLASSIFIED:
            continue
        outcome = _classify_deterministic(
            session, txn, user_rules=user_rules, builtin_rules=builtin_rules, dry_run=True
        )
        if outcome is None:
            pending.setdefault(txn.merchant_key, []).append(txn.description_clean)

    representative = [_select_representative(pending[key]) for key in sorted(pending)]
    return preview_payload(representative)


def apply_user_correction(
    session: Session,
    transaction_id: int,
    *,
    category_key: str,
    subcategory_key: str,
    kind: Kind | None,
    create_rule: bool,
) -> Rule | None:
    """Apply a user's edit to one transaction (I6, §3.12a). Does not commit.

    The edit is a **one-time patch**: the transaction gets the category (and `kind`, if given),
    `confidence=1.0`, `needs_review=False` and `classified_by='user'`, and nothing else changes —
    no `merchant_map` row, no rule. Every later automated pass (re-classify, rule application,
    re-import, the LLM) leaves it alone, and no other transaction of the merchant is affected.

    With `create_rule=True` this is also the explicit "always categorize this merchant this way"
    action: a `rules` row (`source='user'`, `match_type='exact'`, matched against `merchant_key`)
    is created, or the user's existing exact rule for the merchant is updated in place rather than
    duplicated (an older rule would otherwise keep winning by id). The rule applies to the
    merchant's future transactions only; existing ones change only through "Apply to existing".
    Returns the created or updated rule, or `None`.

    Raises:
        ConfigError: `transaction_id` does not exist, or `category_key`/`subcategory_key` is not
            a valid taxonomy pair.
    """
    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise ConfigError(f"no transaction with id {transaction_id}")

    category = session.execute(
        select(Category).where(Category.key == category_key)
    ).scalar_one_or_none()
    if category is None:
        raise ConfigError(f"unknown category {category_key!r}")
    subcategory = session.execute(
        select(Subcategory).where(
            Subcategory.category_id == category.id, Subcategory.key == subcategory_key
        )
    ).scalar_one_or_none()
    if subcategory is None:
        raise ConfigError(f"unknown subcategory {subcategory_key!r} for category {category_key!r}")

    _write_user_patch(
        session,
        txn,
        category_id=category.id,
        subcategory_id=subcategory.id,
        category_key=category_key,
        subcategory_key=subcategory_key,
        kind=kind,
    )

    rule: Rule | None = None
    if create_rule and txn.merchant_key:
        rule = _upsert_exact_rule(
            session,
            user_id=txn.user_id,
            merchant_key=txn.merchant_key,
            category_id=category.id,
            subcategory_id=subcategory.id,
            kind=kind,
        )
    session.flush()
    return rule


def apply_kind_edit(session: Session, txn: Transaction, kind: Kind) -> None:
    """Apply a kind-only edit to `txn` and keep its category consistent with the new kind. Does
    not commit.

    * Spend -> payment/transfer: a row the user has not categorized by hand is filed under
      `others/payments_transfers` exactly as the kind step would (`classified_by='kind'`).
    * Payment/transfer -> spend: the `payments_transfers` filing came from the kind step, not
      from the user, so it is cleared and the deterministic cascade (user rules, user merchant
      map, built-in rules, issuer category, cache; never the LLM, which a kind edit does not
      trigger) is re-run for this one row. When nothing places it the row is left in
      `others/uncategorized` and flagged `needs_review`, still open to a later classify pass.
    * Otherwise a row with a real category becomes a one-time edit (`classified_by='user'`);
      an uncategorized row stays open so rules and the LLM can still fill it in.

    A row that is already `classified_by='user'` keeps its category in every case.
    """
    if kind == txn.kind:
        return
    was_non_spend = txn.kind in _NON_SPEND_KINDS
    txn.kind = kind
    txn.is_spend = kind_is_spend(kind)
    txn.updated_at = utcnow_iso()
    if txn.classified_by == USER_CLASSIFIED:
        return

    if kind in _NON_SPEND_KINDS:
        _write_outcome(
            session,
            txn,
            _Outcome(
                category_key=_PAYMENTS_TRANSFERS_CATEGORY,
                subcategory_key=_PAYMENTS_TRANSFERS_SUBCATEGORY,
                confidence=1.0,
                classified_by="kind",
            ),
            needs_review=False,
        )
        return

    if was_non_spend or txn.classified_by == "kind":
        user_rules, builtin_rules = _load_rule_sets(session)
        outcome = _classify_deterministic(
            session, txn, user_rules=user_rules, builtin_rules=builtin_rules
        )
        if outcome is not None:
            _write_outcome(
                session,
                txn,
                outcome,
                needs_review=outcome.needs_review_override,
            )
            return
        txn.category_id = None
        txn.subcategory_id = None
        txn.confidence = None
        txn.classified_by = None
        txn.needs_review = True
        _park_uncategorized(session, txn)
        return

    if _has_real_category(session, txn):
        txn.classified_by = USER_CLASSIFIED


def _has_real_category(session: Session, txn: Transaction) -> bool:
    """True if `txn` has a category other than the `others/uncategorized` fallback."""
    if txn.category_id is None or txn.subcategory_id is None:
        return False
    category = session.get(Category, txn.category_id)
    sub = session.get(Subcategory, txn.subcategory_id)
    if category is None or sub is None:
        return False
    return (category.key, sub.key) != (_UNCATEGORIZED_CATEGORY, _UNCATEGORIZED_SUBCATEGORY)


def _park_uncategorized(session: Session, txn: Transaction) -> None:
    category = session.execute(
        select(Category).where(Category.key == _UNCATEGORIZED_CATEGORY)
    ).scalar_one()
    subcategory = session.execute(
        select(Subcategory).where(
            Subcategory.category_id == category.id,
            Subcategory.key == _UNCATEGORIZED_SUBCATEGORY,
        )
    ).scalar_one()
    txn.category_id = category.id
    txn.subcategory_id = subcategory.id


def _write_user_patch(
    session: Session,
    txn: Transaction,
    *,
    category_id: int,
    subcategory_id: int,
    category_key: str,
    subcategory_key: str,
    kind: Kind | None,
) -> None:
    """Make `txn` a one-time user edit: the chosen category (and kind), marked
    `classified_by='user'`, no longer flagged for review. Touches no other row."""
    if kind is not None and kind != txn.kind:
        txn.kind = kind
        txn.is_spend = kind_is_spend(kind)

    txn.category_id = category_id
    txn.subcategory_id = subcategory_id
    txn.confidence = 1.0
    txn.classified_by = USER_CLASSIFIED
    txn.needs_review = False
    txn.updated_at = utcnow_iso()
    session.add(
        Classification(
            transaction_id=txn.id,
            category_key=category_key,
            subcategory_key=subcategory_key,
            confidence=1.0,
            method=USER_CLASSIFIED,
            llm_run_id=None,
        )
    )


def _upsert_exact_rule(
    session: Session,
    *,
    user_id: int,
    merchant_key: str,
    category_id: int,
    subcategory_id: int,
    kind: Kind | None,
) -> Rule:
    """The user's exact rule for `merchant_key`: updated in place if one exists (and re-enabled),
    else created at priority 50."""
    rule = (
        session.execute(
            select(Rule)
            .where(
                Rule.source == "user",
                Rule.match_type == "exact",
                Rule.pattern == merchant_key,
                Rule.user_id == user_id,
            )
            .order_by(Rule.id)
        )
        .scalars()
        .first()
    )
    if rule is None:
        rule = Rule(
            user_id=user_id,
            match_type="exact",
            pattern=merchant_key,
            category_id=category_id,
            subcategory_id=subcategory_id,
            kind_override=kind,
            priority=50,
            enabled=True,
            source="user",
        )
        session.add(rule)
    else:
        rule.category_id = category_id
        rule.subcategory_id = subcategory_id
        rule.kind_override = kind
        rule.enabled = True
    return rule


# --------------------------------------------------------------------------------------------
# Applying a rule to transactions that already exist.
# --------------------------------------------------------------------------------------------


def rule_target_ids(session: Session, rule: Rule) -> list[int]:
    """Ids of existing transactions `rule` would reclassify if applied now.

    A transaction qualifies when `rule` is the *first* enabled user rule matching its
    `merchant_key` (so a higher-priority rule is never overridden), it is not a payment/transfer
    (those never reach rules, step 1), the user has not corrected it by hand
    (`classified_by='user'` is final, I6), and applying the rule would actually change it.
    """
    if rule.source != "user" or not rule.enabled:
        return []
    user_rules, _ = _load_rule_sets(session)

    candidate_keys = session.execute(
        select(Transaction.merchant_key)
        .where(
            Transaction.kind.not_in(tuple(_NON_SPEND_KINDS)),
            Transaction.classified_by.is_distinct_from(USER_CLASSIFIED),
        )
        .distinct()
    ).scalars()
    matching_keys = [key for key in candidate_keys if key and _safe_rule_matches(rule, key)]

    targets: list[int] = []
    for start in range(0, len(matching_keys), 500):
        chunk = matching_keys[start : start + 500]
        rows = session.execute(
            select(Transaction).where(
                Transaction.merchant_key.in_(chunk),
                Transaction.kind.not_in(tuple(_NON_SPEND_KINDS)),
                Transaction.classified_by.is_distinct_from(USER_CLASSIFIED),
            )
        ).scalars()
        for txn in rows:
            if not _rule_applies_to_user(rule, txn.user_id):
                continue
            first = next(
                (
                    candidate
                    for candidate in user_rules
                    if _rule_applies_to_user(candidate, txn.user_id)
                    and _safe_rule_matches(candidate, txn.merchant_key)
                ),
                None,
            )
            if first is None or first.id != rule.id:
                continue
            already = (
                txn.category_id == rule.category_id
                and txn.subcategory_id == rule.subcategory_id
                and not txn.needs_review
                and (rule.kind_override is None or rule.kind_override == txn.kind)
            )
            if not already:
                targets.append(txn.id)
    return targets


@dataclass(frozen=True, slots=True)
class RuleApplyPreview:
    """What `apply_rule_to_existing` would do right now, for the confirmation step: how many
    transactions change, how many distinct users own them (a rule with no owner spans all of
    them), and how many would get a different `kind`."""

    matches: int
    user_count: int
    kind_changes: int
    all_users: bool


def preview_rule_application(session: Session, rule: Rule) -> RuleApplyPreview:
    """Count what `apply_rule_to_existing(session, rule)` would change, without changing it."""
    targets = [session.get(Transaction, txn_id) for txn_id in rule_target_ids(session, rule)]
    rows = [t for t in targets if t is not None]
    kind_changes = sum(
        1 for t in rows if rule.kind_override is not None and rule.kind_override != t.kind
    )
    return RuleApplyPreview(
        matches=len(rows),
        user_count=len({t.user_id for t in rows}),
        kind_changes=kind_changes,
        all_users=rule.user_id is None,
    )


def apply_rule_to_existing(session: Session, rule: Rule) -> tuple[int, AcceptUndo]:
    """Apply `rule` to every transaction `rule_target_ids` selects (the rule's category, its kind
    override if any, `classified_by='user_rule'`, no longer flagged for review). Returns how many
    were changed and an `AcceptUndo` (the same token Review's accept returns, so
    `undo_accept` / `POST /review/undo` reverses it; it carries no rule). Never touches a
    user-corrected row. Does not commit."""
    snapshots: list[TransactionSnapshot] = []
    for txn_id in rule_target_ids(session, rule):
        txn = session.get(Transaction, txn_id)
        if txn is None:  # pragma: no cover - defensive
            continue
        before = _state_of(txn)
        _write_outcome(
            session,
            txn,
            _outcome_from_rule(session, rule, classified_by="user_rule"),
            needs_review=False,
        )
        snapshots.append(TransactionSnapshot(txn.id, before, _state_of(txn), txn.dedupe_hash))
    session.flush()
    return len(snapshots), AcceptUndo(transactions=tuple(snapshots), rule=None)


def _safe_rule_matches(rule: Rule, merchant_key: str) -> bool:
    """`rule_matches`, but a stored regex that no longer compiles matches nothing."""
    try:
        return rule_matches(rule, merchant_key)
    except re.error:
        return False


# --------------------------------------------------------------------------------------------
# Review: accept (optionally for the merchant's other pending rows) and undo.
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransactionState:
    """The fields one accept (or one "apply to existing") can change on a transaction."""

    category_id: int | None
    subcategory_id: int | None
    confidence: float | None
    classified_by: str | None
    needs_review: bool
    kind: str
    is_spend: bool
    merchant_canonical: str | None


@dataclass(frozen=True, slots=True)
class TransactionSnapshot:
    """One transaction as an operation found it (`before`) and left it (`after`). Undo restores
    `before` only if the row still equals `after`: a later edit by the user is never reverted."""

    transaction_id: int
    before: TransactionState
    after: TransactionState
    #: The row's `dedupe_hash`: ids can be reused after a delete (no AUTOINCREMENT), so undo
    #: refuses a row whose hash differs.
    dedupe_hash: str


@dataclass(frozen=True, slots=True)
class RuleState:
    category_id: int
    subcategory_id: int
    kind_override: str | None
    enabled: bool


@dataclass(frozen=True, slots=True)
class RuleSnapshot:
    """The merchant's exact rule around an accept. `before=None` means the accept created it, so
    undo deletes it; otherwise undo puts `before` back. Either way only if the rule still equals
    `after` and is the user's exact rule for the accepted transaction's merchant."""

    rule_id: int
    before: RuleState | None
    after: RuleState


@dataclass(frozen=True, slots=True)
class AcceptUndo:
    """Everything one `accept_review` (or `apply_rule_to_existing`) changed, to hand back to
    `undo_accept`: every patched transaction before and after and, when the accept was an "always
    categorize" one, the rule."""

    transactions: tuple[TransactionSnapshot, ...]
    rule: RuleSnapshot | None


@dataclass(frozen=True, slots=True)
class UndoResult:
    """What `undo_accept` did: the transactions put back, the ones skipped because they changed
    (or vanished) since, and whether the rule was left alone for the same reason."""

    restored: tuple[int, ...]
    skipped: tuple[int, ...]
    rule_skipped: bool


@dataclass(frozen=True, slots=True)
class AcceptResult:
    """The outcome of `accept_review`: the extra rows it applied to, and how to undo it all."""

    transaction_id: int
    similar_ids: tuple[int, ...]
    undo: AcceptUndo


# `CLASSIFIED_BY_VALUES` (imported from `core.types`) is the set an undo token may name; anything
# else is forged or corrupt.
_KIND_VALUES: frozenset[str] = frozenset(
    {"purchase", "refund", "payment", "transfer", "fee", "interest", "adjustment"}
)


def _state_of(txn: Transaction) -> TransactionState:
    return TransactionState(
        category_id=txn.category_id,
        subcategory_id=txn.subcategory_id,
        confidence=txn.confidence,
        classified_by=txn.classified_by,
        needs_review=txn.needs_review,
        kind=txn.kind,
        is_spend=txn.is_spend,
        merchant_canonical=txn.merchant_canonical,
    )


def _rule_state_of(rule: Rule) -> RuleState:
    return RuleState(
        category_id=rule.category_id,
        subcategory_id=rule.subcategory_id,
        kind_override=rule.kind_override,
        enabled=rule.enabled,
    )


def accept_review(
    session: Session,
    transaction_id: int,
    *,
    category_key: str,
    subcategory_key: str,
    create_rule: bool,
    similar_ids: Sequence[int] = (),
    kind: Kind | None = None,
) -> AcceptResult:
    """Accept one Review-queue transaction as `category_key/subcategory_key`.

    By default this is a one-time patch of that transaction (`apply_user_correction` with no
    rule): nothing is remembered about the merchant. With `create_rule` it is the explicit
    "always categorize this merchant this way" action, which also saves the merchant's exact
    rule. `similar_ids` — other still-pending rows of the *same merchant* the user is looking
    at — are simply patched the same way (each one is its own one-time edit; they do not become a
    rule).

    A `similar_ids` entry is skipped unless it exists, has the accepted row's non-empty
    `merchant_key`, is still `needs_review`, and was not already edited by hand. Returns the ids
    actually applied and an `AcceptUndo` that `undo_accept` can replay. Does not commit.

    `kind` (optional) is also set on the accepted transaction itself, as a one-time edit, so a
    one-click suggestion from a rule with a kind override does what that rule would; it never
    reaches `similar_ids`. Accepting is also how a suggestion (`classify.candidates`) is applied
    with an undo token: it is the same one-time patch as `PATCH /transactions/{id}`.

    Raises:
        ConfigError: the transaction or taxonomy pair does not exist (see `apply_user_correction`).
    """
    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise ConfigError(f"no transaction with id {transaction_id}")

    before_main = _state_of(txn)
    rule_before = (
        session.execute(
            select(Rule)
            .where(
                Rule.source == "user",
                Rule.match_type == "exact",
                Rule.pattern == txn.merchant_key,
                Rule.user_id == txn.user_id,
            )
            .order_by(Rule.id)
        )
        .scalars()
        .first()
        if create_rule and txn.merchant_key
        else None
    )
    rule_state_before = None if rule_before is None else _rule_state_of(rule_before)

    rule = apply_user_correction(
        session,
        transaction_id,
        category_key=category_key,
        subcategory_key=subcategory_key,
        kind=kind,
        create_rule=create_rule,
    )
    snapshots = [TransactionSnapshot(txn.id, before_main, _state_of(txn), txn.dedupe_hash)]
    rule_snapshot = (
        RuleSnapshot(rule_id=rule.id, before=rule_state_before, after=_rule_state_of(rule))
        if rule is not None
        else None
    )

    applied: list[int] = []
    for similar_id in dict.fromkeys(similar_ids):
        other = session.get(Transaction, similar_id)
        if (
            other is None
            or other.id == txn.id
            or not txn.merchant_key
            or other.merchant_key != txn.merchant_key
            or not other.needs_review
            or other.classified_by == USER_CLASSIFIED
        ):
            continue
        before_other = _state_of(other)
        apply_user_correction(
            session,
            other.id,
            category_key=category_key,
            subcategory_key=subcategory_key,
            kind=None,
            create_rule=False,
        )
        snapshots.append(
            TransactionSnapshot(other.id, before_other, _state_of(other), other.dedupe_hash)
        )
        applied.append(other.id)
    session.flush()
    return AcceptResult(
        transaction_id=transaction_id,
        similar_ids=tuple(applied),
        undo=AcceptUndo(transactions=tuple(snapshots), rule=rule_snapshot),
    )


@dataclass(frozen=True, slots=True)
class RevertResult:
    """The outcome of `revert_to_automatic`: which rows went back to automatic classification,
    which were left alone, and the token (`AcceptUndo`, so `undo_accept` replays it) that puts
    every reverted row back as the one-time edit it was (only if it still equals what the revert
    left: an edit made afterwards is never overwritten)."""

    reverted_ids: tuple[int, ...]
    skipped_ids: tuple[int, ...]
    undo: AcceptUndo


def revert_to_automatic(
    session: Session, transaction_ids: Sequence[int], *, confidence_threshold: float
) -> RevertResult:
    """Hand one-time-edited rows back to automatic classification. Does not commit.

    A one-time edit (`classified_by='user'`) is otherwise final: no rule change, re-classify or
    re-import ever reconsiders the row. This is the one explicit way out. For each listed row that
    carries an edit, the edit is dropped and the **deterministic** cascade (steps 1-6: kind,
    your rules, legacy pins, built-in rules, issuer label, learned merchant cache / saved LLM
    answer) is run for that row alone and its answer written with the usual provenance
    (`user_rule`, `builtin_rule`, ..., `cache`). If no deterministic layer has an answer, the row
    gets what the LLM phase writes for a row it could not place: `others/uncategorized`,
    confidence 0, `needs_review=True` (a later "Re-classify" may still ask the model, because
    that is a separate, explicit action). **This never calls the LLM or any network service**,
    and it writes no `merchant_map` row and no hit count (`dry_run`), so the undo token is exact.

    `kind` is not reverted: the parser's original kind is not stored, so a kind you changed by
    hand stays; a rule's kind override still applies, as it would in a normal run. Ids that do
    not exist, or are not currently edited by hand, are skipped (reported in `skipped_ids`).
    `confidence_threshold` is the LLM setting `needs_review` is judged against.
    """
    user_rules, builtin_rules = _load_rule_sets(session)
    reverted: list[int] = []
    skipped: list[int] = []
    snapshots: list[TransactionSnapshot] = []
    for transaction_id in dict.fromkeys(transaction_ids):
        txn = session.get(Transaction, transaction_id)
        if txn is None or txn.classified_by != USER_CLASSIFIED:
            skipped.append(transaction_id)
            continue
        before = _state_of(txn)
        outcome = _classify_deterministic(
            session, txn, user_rules=user_rules, builtin_rules=builtin_rules, dry_run=True
        )
        if outcome is None:
            outcome = _Outcome(
                category_key=_UNCATEGORIZED_CATEGORY,
                subcategory_key=_UNCATEGORIZED_SUBCATEGORY,
                confidence=0.0,
                # Same provenance the import path writes for a row the LLM phase could not
                # place (batching's placeholder `Item` goes through `_outcome_from_item`).
                classified_by="llm",
                needs_review_override=True,
            )
        below_threshold = (
            outcome.confidence is not None and outcome.confidence < confidence_threshold
        )
        # The edit is dropped first so the row is an ordinary automatic one even if the write
        # below raises half-way (the caller rolls the session back in that case).
        txn.classified_by = None
        _write_outcome(
            session,
            txn,
            outcome,
            needs_review=outcome.needs_review_override or below_threshold,
        )
        snapshots.append(TransactionSnapshot(txn.id, before, _state_of(txn), txn.dedupe_hash))
        reverted.append(txn.id)
    session.flush()
    return RevertResult(
        reverted_ids=tuple(reverted),
        skipped_ids=tuple(skipped),
        undo=AcceptUndo(transactions=tuple(snapshots), rule=None),
    )


def validate_undo(session: Session, undo: AcceptUndo) -> None:
    """Reject an undo token that could not have come from this server: an unknown kind or
    `classified_by`, a confidence outside 0..1, or a category/subcategory id that does not exist
    or does not belong together. The token is held by the client, so nothing in it is trusted.

    Raises:
        ConfigError: the token is invalid. Nothing has been changed.
    """
    pairs: set[tuple[int | None, int | None]] = set()
    states: list[TransactionState] = []
    for snap in undo.transactions:
        states.extend((snap.before, snap.after))
    for state in states:
        if state.kind not in _KIND_VALUES:
            raise ConfigError(f"invalid undo token: unknown kind {state.kind!r}")
        if state.classified_by is not None and state.classified_by not in CLASSIFIED_BY_VALUES:
            raise ConfigError(f"invalid undo token: unknown classified_by {state.classified_by!r}")
        if state.confidence is not None and not 0.0 <= state.confidence <= 1.0:
            raise ConfigError("invalid undo token: confidence out of range")
        pairs.add((state.category_id, state.subcategory_id))
    if undo.rule is not None:
        rule_states = [undo.rule.after] + ([undo.rule.before] if undo.rule.before else [])
        for rule_state in rule_states:
            if (
                rule_state.kind_override is not None
                and rule_state.kind_override not in _KIND_VALUES
            ):
                raise ConfigError("invalid undo token: unknown kind override")
            pairs.add((rule_state.category_id, rule_state.subcategory_id))
    for category_id, subcategory_id in pairs:
        if category_id is None and subcategory_id is None:
            continue
        if category_id is None or subcategory_id is None:
            raise ConfigError("invalid undo token: category without subcategory")
        sub = session.get(Subcategory, subcategory_id)
        if sub is None or sub.category_id != category_id:
            raise ConfigError("invalid undo token: unknown category/subcategory pair")


def undo_accept(session: Session, undo: AcceptUndo) -> UndoResult:
    """Reverse one `accept_review` (or `apply_rule_to_existing`) without clobbering anything the
    user did afterwards.

    Each snapshotted transaction goes back to its `before` state only if it still equals its
    `after` state; one edited since (re-categorized, relabelled, re-accepted, deleted) is skipped
    and reported. The rule is touched only if it is the user's exact rule for the accepted
    transaction's merchant and still equals what the accept left: then it is put back, or deleted
    if the accept created it; a rule edited since is left alone and reported. The
    `classifications` audit rows the accept wrote stay (history, never read back). Does not
    commit.

    Raises:
        ConfigError: `undo` fails `validate_undo` (nothing is changed).
    """
    validate_undo(session, undo)
    restored: list[int] = []
    skipped: list[int] = []
    first_txn: Transaction | None = None
    for index, snap in enumerate(undo.transactions):
        txn = session.get(Transaction, snap.transaction_id)
        if index == 0:
            first_txn = txn
        if txn is None or txn.dedupe_hash != snap.dedupe_hash or _state_of(txn) != snap.after:
            skipped.append(snap.transaction_id)
            continue
        before = snap.before
        txn.category_id = before.category_id
        txn.subcategory_id = before.subcategory_id
        txn.confidence = before.confidence
        txn.classified_by = before.classified_by
        txn.needs_review = before.needs_review
        txn.kind = before.kind
        txn.is_spend = before.is_spend
        txn.merchant_canonical = before.merchant_canonical
        txn.updated_at = utcnow_iso()
        restored.append(txn.id)

    rule_skipped = False
    rule_snap = undo.rule
    if rule_snap is not None:
        rule_row = session.get(Rule, rule_snap.rule_id)
        if (
            rule_row is None
            or first_txn is None
            or rule_row.source != "user"
            or rule_row.match_type != "exact"
            or rule_row.user_id != first_txn.user_id
            or rule_row.pattern != first_txn.merchant_key
            or _rule_state_of(rule_row) != rule_snap.after
        ):
            rule_skipped = True
        elif rule_snap.before is None:
            session.delete(rule_row)
        else:
            rule_row.category_id = rule_snap.before.category_id
            rule_row.subcategory_id = rule_snap.before.subcategory_id
            rule_row.kind_override = rule_snap.before.kind_override
            rule_row.enabled = rule_snap.before.enabled
    session.flush()
    return UndoResult(restored=tuple(restored), skipped=tuple(skipped), rule_skipped=rule_skipped)


def approve_subcategory(session: Session, subcategory_id: int) -> None:
    """Flip a `pending_approval` dynamic subcategory to `active` (§2b P2-B). Does not commit.

    Raises:
        ConfigError: no such subcategory.
    """
    subcategory = session.get(Subcategory, subcategory_id)
    if subcategory is None:
        raise ConfigError(f"no subcategory with id {subcategory_id}")
    subcategory.status = "active"
    session.flush()


def merge_subcategory(session: Session, subcategory_id: int, *, into_id: int) -> None:
    """Merge `subcategory_id` into `into_id`, atomically (A14). Does not commit.

    Rewrites every `transactions` row, `merchant_map` row and `rules` row that references
    `subcategory_id` to reference `into_id` (and `into_id`'s category) instead, then marks
    `subcategory_id` as `merged`, recording `merged_into`. Future cache and rule hits resolve
    straight to `into_id`. A transaction the user edited by hand is *remapped* like any other —
    it keeps its `classified_by='user'`, `confidence` and `needs_review`, and just follows the
    category it pointed at into its new home; a merge never resets an edit.

    Raises:
        ConfigError: either id does not exist, or `into_id` is itself already merged away.
    """
    source = session.get(Subcategory, subcategory_id)
    target = session.get(Subcategory, into_id)
    if source is None:
        raise ConfigError(f"no subcategory with id {subcategory_id}")
    if target is None:
        raise ConfigError(f"no subcategory with id {into_id}")
    if target.status == "merged":
        raise ConfigError(f"subcategory {into_id} is itself merged; merge into its target instead")

    now = utcnow_iso()
    for txn in session.execute(
        select(Transaction).where(Transaction.subcategory_id == subcategory_id)
    ).scalars():
        txn.subcategory_id = target.id
        txn.category_id = target.category_id
        txn.updated_at = now

    for row in session.execute(
        select(MerchantMap).where(MerchantMap.subcategory_id == subcategory_id)
    ).scalars():
        row.subcategory_id = target.id
        row.category_id = target.category_id
        row.updated_at = now

    for rule_row in session.execute(
        select(Rule).where(Rule.subcategory_id == subcategory_id)
    ).scalars():
        rule_row.subcategory_id = target.id
        rule_row.category_id = target.category_id

    source.status = "merged"
    source.merged_into = target.id
    session.flush()


# --------------------------------------------------------------------------------------------
# Deterministic cascade, steps 1-6.
# --------------------------------------------------------------------------------------------


def _load_rule_sets(session: Session) -> tuple[list[Rule], list[Rule]]:
    user_rules = list(
        session.execute(
            select(Rule)
            .where(Rule.source == "user", Rule.enabled.is_(True))
            .order_by(Rule.priority, Rule.id)
        ).scalars()
    )
    builtin_rules = list(
        session.execute(
            select(Rule)
            .where(Rule.source == "builtin", Rule.enabled.is_(True))
            .order_by(Rule.priority, Rule.id)
        ).scalars()
    )
    return user_rules, builtin_rules


#: `merchant_map.source` values the cascade reads at each step, shared with
#: `classify.candidates` so the one-click suggestions can never read a different set of rows.
USER_PIN_SOURCES: tuple[str, ...] = ("user",)
CACHE_SOURCES: tuple[str, ...] = ("llm", "issuer")


def first_matching_rule(
    rules: Sequence[Rule],
    *,
    user_id: int,
    merchant_key: str,
    matches: Callable[[Rule, str], bool] = rule_matches,
) -> Rule | None:
    """The first rule of `rules` (already in priority order) that applies to `user_id` and whose
    pattern matches `merchant_key`: cascade steps 2 and 4 (and their suggestions). `matches` lets
    a read-only caller swap in a matcher that treats a stored regex that no longer compiles as a
    non-match instead of raising."""
    for rule in rules:
        if _rule_applies_to_user(rule, user_id) and matches(rule, merchant_key):
            return rule
    return None


def issuer_match(parser_id: str | None, issuer_category: str | None) -> IssuerCategoryMatch | None:
    """Cascade step 5's lookup: the category an issuer's own label implies, or `None`."""
    if not issuer_category:
        return None
    return resolve_issuer_category(parser_id, issuer_category)


def _classify_deterministic(
    session: Session,
    txn: Transaction,
    *,
    user_rules: Sequence[Rule],
    builtin_rules: Sequence[Rule],
    dry_run: bool = False,
) -> _Outcome | None:
    """Cascade steps 1-6 for one transaction. `None` means it must go to the LLM (step 7).

    `dry_run=True` (used by `preview_egress`) performs every read but skips every write
    (`merchant_map.hit_count` increments, the issuer-category `merchant_map` write), so nothing
    persists from a preview.

    The per-layer matching goes through `first_matching_rule`, `issuer_match` and the
    `USER_PIN_SOURCES` / `CACHE_SOURCES` constants, which `classify.candidates` reuses.
    """
    if txn.kind in _NON_SPEND_KINDS:
        return _Outcome(
            category_key=_PAYMENTS_TRANSFERS_CATEGORY,
            subcategory_key=_PAYMENTS_TRANSFERS_SUBCATEGORY,
            confidence=1.0,
            classified_by="kind",
        )

    user_rule = first_matching_rule(user_rules, user_id=txn.user_id, merchant_key=txn.merchant_key)
    if user_rule is not None:
        return _outcome_from_rule(session, user_rule, classified_by="user_rule")

    if txn.merchant_key:
        user_map = session.execute(
            select(MerchantMap).where(
                MerchantMap.merchant_key == txn.merchant_key,
                MerchantMap.source.in_(USER_PIN_SOURCES),
            )
        ).scalar_one_or_none()
        if user_map is not None:
            if not dry_run:
                user_map.hit_count += 1
            # Recorded as a rule result, never as a one-time edit (`classified_by='user'`).
            return _outcome_from_map(session, user_map, classified_by="user_rule")

    builtin_rule = first_matching_rule(
        builtin_rules, user_id=txn.user_id, merchant_key=txn.merchant_key
    )
    if builtin_rule is not None:
        return _outcome_from_rule(session, builtin_rule, classified_by="builtin_rule")

    parser_id = txn.statement.parser_id if txn.statement is not None else None
    match = issuer_match(parser_id, txn.issuer_category)
    if match is not None:
        outcome = _Outcome(
            category_key=match.category_key,
            subcategory_key=match.subcategory_key,
            confidence=0.9,
            classified_by="issuer",
        )
        if txn.merchant_key and not dry_run:
            _write_merchant_map(
                session,
                txn.merchant_key,
                outcome,
                source="issuer",
                canonical=txn.merchant_canonical,
            )
        return outcome

    if txn.merchant_key:
        cache_map = session.execute(
            select(MerchantMap).where(
                MerchantMap.merchant_key == txn.merchant_key,
                MerchantMap.source.in_(CACHE_SOURCES),
            )
        ).scalar_one_or_none()
        if cache_map is not None:
            if not dry_run:
                cache_map.hit_count += 1
            return _outcome_from_map(session, cache_map, classified_by="cache")

    return None


def _rule_applies_to_user(rule: Rule, user_id: int) -> bool:
    return rule.user_id is None or rule.user_id == user_id


def _outcome_from_rule(session: Session, rule: Rule, *, classified_by: str) -> _Outcome:
    category = session.get(Category, rule.category_id)
    subcategory = session.get(Subcategory, rule.subcategory_id)
    if category is None or subcategory is None:
        raise ConfigError(f"rule {rule.id} references a missing category/subcategory")
    return _Outcome(
        category_key=category.key,
        subcategory_key=subcategory.key,
        confidence=1.0,
        classified_by=classified_by,
        kind_override=rule.kind_override,
    )


def _outcome_from_map(session: Session, row: MerchantMap, *, classified_by: str) -> _Outcome:
    category = session.get(Category, row.category_id)
    subcategory = session.get(Subcategory, row.subcategory_id)
    if category is None or subcategory is None:
        raise ConfigError(
            f"merchant_map {row.merchant_key!r} references a missing category/subcategory"
        )
    return _Outcome(
        category_key=category.key,
        subcategory_key=subcategory.key,
        confidence=row.confidence,
        classified_by=classified_by,
        merchant_canonical=row.merchant_canonical,
    )


def _write_merchant_map(
    session: Session,
    merchant_key: str,
    outcome: _Outcome,
    *,
    source: str,
    canonical: str | None = None,
) -> None:
    """Upsert `merchant_map[merchant_key]` from `outcome`. Never overwrites a `source='user'`
    row (I6), and never writes an empty `merchant_key` (A27, guarded by callers)."""
    if not merchant_key:
        return
    category = session.execute(
        select(Category).where(Category.key == outcome.category_key)
    ).scalar_one()
    subcategory = session.execute(
        select(Subcategory).where(
            Subcategory.category_id == category.id, Subcategory.key == outcome.subcategory_key
        )
    ).scalar_one()

    row = session.get(MerchantMap, merchant_key)
    if row is not None and row.source == "user":
        return
    resolved_canonical = canonical or outcome.merchant_canonical
    if row is None:
        session.add(
            MerchantMap(
                merchant_key=merchant_key,
                merchant_canonical=resolved_canonical,
                category_id=category.id,
                subcategory_id=subcategory.id,
                source=source,
                confidence=outcome.confidence,
                hit_count=0,
                updated_at=utcnow_iso(),
            )
        )
    else:
        row.merchant_canonical = resolved_canonical or row.merchant_canonical
        row.category_id = category.id
        row.subcategory_id = subcategory.id
        row.source = source
        row.confidence = outcome.confidence
        row.updated_at = utcnow_iso()


def _apply_outcome(
    session: Session,
    txn: Transaction,
    outcome: _Outcome,
    *,
    cfg: LLMConfig,
    llm_run_id: int | None = None,
) -> None:
    below_threshold = (
        outcome.confidence is not None and outcome.confidence < cfg.confidence_threshold
    )
    _write_outcome(
        session,
        txn,
        outcome,
        needs_review=outcome.needs_review_override or below_threshold,
        llm_run_id=llm_run_id,
    )


def _write_outcome(
    session: Session,
    txn: Transaction,
    outcome: _Outcome,
    *,
    needs_review: bool,
    llm_run_id: int | None = None,
) -> None:
    """Persist `outcome` onto `txn` (category, kind override, provenance) and log it."""
    category = session.execute(
        select(Category).where(Category.key == outcome.category_key)
    ).scalar_one()
    subcategory = session.execute(
        select(Subcategory).where(
            Subcategory.category_id == category.id, Subcategory.key == outcome.subcategory_key
        )
    ).scalar_one()

    if outcome.kind_override is not None and outcome.kind_override != txn.kind:
        txn.kind = outcome.kind_override
        txn.is_spend = kind_is_spend(cast(Kind, outcome.kind_override))

    txn.category_id = category.id
    txn.subcategory_id = subcategory.id
    txn.confidence = outcome.confidence
    txn.classified_by = outcome.classified_by
    txn.needs_review = needs_review
    if outcome.merchant_canonical:
        txn.merchant_canonical = outcome.merchant_canonical
    txn.updated_at = utcnow_iso()
    session.add(
        Classification(
            transaction_id=txn.id,
            category_key=outcome.category_key,
            subcategory_key=outcome.subcategory_key,
            confidence=outcome.confidence,
            method=outcome.classified_by,
            llm_run_id=llm_run_id,
        )
    )


# --------------------------------------------------------------------------------------------
# LLM batch (step 7) and dynamic online_shopping subcategories.
# --------------------------------------------------------------------------------------------


def _description_of(session: Session, transaction_id: int) -> str | None:
    txn = session.get(Transaction, transaction_id)
    return txn.description_clean if txn is not None else None


def _select_representative(descriptions: Sequence[str]) -> str:
    """A5: the modal `description_clean` among transactions sharing one `merchant_key`, tied
    broken by shortest then lexicographic. `descriptions` is never empty for a real merchant
    key (every key came from at least one transaction)."""
    counts = Counter(descriptions)
    max_count = max(counts.values())
    candidates = [d for d, c in counts.items() if c == max_count]
    candidates.sort(key=lambda d: (len(d), d))
    return candidates[0]


def _outcome_from_item(session: Session, item: Item, *, batch_flagged: bool) -> _Outcome:
    """Turn one LLM `Item` into an `_Outcome`, resolving a dynamic `online_shopping`
    subcategory (create-or-merge-follow) as needed."""
    if is_dynamic(item.category):
        subcategory = _resolve_dynamic_subcategory(
            session, item.category, item.merchant_canonical or item.subcategory
        )
        subcategory_key = subcategory.key
        pending = subcategory.status == "pending_approval"
    else:
        subcategory_key = item.subcategory
        pending = False

    return _Outcome(
        category_key=item.category,
        subcategory_key=subcategory_key,
        confidence=item.confidence,
        classified_by="llm",
        merchant_canonical=item.merchant_canonical or None,
        needs_review_override=batch_flagged or pending,
    )


def _slugify_store_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "unknown"


def _resolve_dynamic_subcategory(
    session: Session, category_key: str, merchant_canonical: str
) -> Subcategory:
    """`online_shopping`'s dynamic subcategory create-or-merge-follow (§2b P2-B):

    - exists and `active` -> use it;
    - exists and `merged` -> follow `merged_into`;
    - new -> insert `pending_approval` (the caller flags the transaction for review).
    """
    category = session.execute(select(Category).where(Category.key == category_key)).scalar_one()
    slug = _slugify_store_name(merchant_canonical)
    subcategory = session.execute(
        select(Subcategory).where(Subcategory.category_id == category.id, Subcategory.key == slug)
    ).scalar_one_or_none()
    if subcategory is None:
        subcategory = Subcategory(
            category_id=category.id,
            key=slug,
            label=merchant_canonical.strip() or slug.replace("_", " ").title(),
            is_dynamic=True,
            status="pending_approval",
        )
        session.add(subcategory)
        session.flush()
        return subcategory
    if subcategory.status == "merged" and subcategory.merged_into is not None:
        target = session.get(Subcategory, subcategory.merged_into)
        if target is not None:
            return target
    return subcategory


def _persist_llm_run(session: Session, result: BatchResult, *, group_id: str) -> int:
    now = utcnow_iso()
    run = LlmRun(
        group_id=group_id,
        kind="classify",
        provider=result.provider,
        model=result.model,
        prompt_version=result.prompt_version,
        schema_mode=result.schema_mode,
        row_count=result.row_count,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        status=result.status,
        error_detail=None,
        request_sha256=result.request_sha256,
        started_at=now,
        finished_at=now,
    )
    session.add(run)
    session.flush()
    return run.id


__all__ = [
    "CACHE_SOURCES",
    "USER_PIN_SOURCES",
    "AcceptResult",
    "AcceptUndo",
    "ClassifyResult",
    "RuleSnapshot",
    "TransactionSnapshot",
    "accept_review",
    "apply_kind_edit",
    "apply_rule_to_existing",
    "apply_user_correction",
    "approve_subcategory",
    "classify_transactions",
    "first_matching_rule",
    "issuer_match",
    "merge_subcategory",
    "preview_egress",
    "rule_target_ids",
    "undo_accept",
]
