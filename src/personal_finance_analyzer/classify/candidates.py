"""One-click category suggestions: what *each* cascade layer would say about a transaction.

The cascade (`classify.cascade`) is first-hit-wins, so when several layers disagree the user only
ever sees the winner. This module evaluates every layer independently, without short-circuiting,
and returns them all, in precedence order, as *candidates*. The UI turns each into a chip; a click
applies that candidate as an ordinary one-time edit (`PATCH /transactions/{id}`), so the higher
layer still wins by default and nothing about the cascade changes.

Layers, highest precedence first (one-time edits are above all of them):

0. `one_time_edit` — the row was patched by hand (`classified_by='user'`). Current by definition.
   The layers below are still listed: "what would automation say if I hadn't edited this?".
1. `kind` — a payment or transfer: the cascade files it under `others/payments_transfers` and
   stops, so this is its *only* layer.
2. `user_rule` — your rules (and, ranked just below them, a legacy `merchant_map` pin).
3. `builtin_rule` — the shipped rules.
4. `issuer` — the category the issuer printed on the statement line.
5. `merchant_cache` / `llm` — the learned cache (`merchant_map`, written by earlier issuer hits
   and LLM runs). One row per merchant, so exactly one of the two: `llm` when the saved answer
   came from the LLM ("LLM (saved)"), `merchant_cache` when it was learned from an issuer label.

Guarantees:

- **No LLM or network call, ever.** The `llm` candidate is a *stored* answer (an LLM-sourced
  `merchant_map` row); this module never imports the LLM client and performs read-only queries.
- **No drift.** Matching uses the cascade's own `first_matching_rule`, `issuer_match`,
  `USER_PIN_SOURCES` and `CACHE_SOURCES`; a test asserts the first candidate equals what the
  cascade assigns.
- **`conflict`** is true when the layers (not counting a one-time edit) name at least two distinct
  category/subcategory pairs for a row that has not been edited by hand. An edit settles the
  disagreement, so an edited row is never flagged.
- `is_current` marks the highest-precedence layer whose answer equals what the row is currently
  filed under (or the one-time edit). If the stored category is stale (a rule was added since),
  no layer is current and the winner is simply the first chip.

Performance: rules are loaded once per call, and each layer's answer is memoized per distinct
`(user, merchant_key)` / `(parser, issuer label)` / `merchant_key`, so a page or a whole filtered
set costs one rule scan per *distinct merchant* (not per row) plus a handful of batched lookups.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Final

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from personal_finance_analyzer.classify import cascade
from personal_finance_analyzer.classify.rules import rule_matches
from personal_finance_analyzer.core.errors import ConfigError
from personal_finance_analyzer.core.types import USER_CLASSIFIED
from personal_finance_analyzer.db.models import (
    Category,
    MerchantMap,
    Rule,
    Statement,
    Subcategory,
    Transaction,
)

SOURCE_ONE_TIME_EDIT: Final = "one_time_edit"
SOURCE_KIND: Final = "kind"
SOURCE_USER_RULE: Final = "user_rule"
SOURCE_BUILTIN_RULE: Final = "builtin_rule"
SOURCE_ISSUER: Final = "issuer"
SOURCE_MERCHANT_CACHE: Final = "merchant_cache"
SOURCE_LLM: Final = "llm"

#: `IN (...)` chunk size for the batched lookups (SQLite's default variable limit is 999+).
_CHUNK = 500

_NON_SPEND_KINDS: Final = frozenset({"payment", "transfer"})


@dataclass(frozen=True, slots=True)
class Candidate:
    """One layer's answer for one transaction."""

    source: str
    category_key: str
    subcategory_key: str
    #: A rule's `kind_override`, applied together with the category when the chip is clicked.
    kind: str | None
    #: Human-readable provenance: a rule's pattern, the issuer's own label, "learned from LLM"...
    label: str
    rule_id: int | None
    is_current: bool

    @property
    def pair(self) -> tuple[str, str]:
        return (self.category_key, self.subcategory_key)


@dataclass(frozen=True, slots=True)
class CandidateSet:
    """Every layer's candidate for one transaction, in precedence order, and whether they
    disagree (see the module docstring for the exact definition of `conflict`)."""

    candidates: tuple[Candidate, ...]
    conflict: bool


@dataclass(frozen=True, slots=True)
class TxnFacts:
    """The handful of `transactions` columns candidate computation reads."""

    id: int
    user_id: int
    statement_id: int
    merchant_key: str
    kind: str
    issuer_category: str | None
    classified_by: str | None
    category_id: int | None
    subcategory_id: int | None


_FACT_COLUMNS = (
    Transaction.id,
    Transaction.user_id,
    Transaction.statement_id,
    Transaction.merchant_key,
    Transaction.kind,
    Transaction.issuer_category,
    Transaction.classified_by,
    Transaction.category_id,
    Transaction.subcategory_id,
)


def facts_of(txn: Transaction) -> TxnFacts:
    return TxnFacts(
        id=txn.id,
        user_id=txn.user_id,
        statement_id=txn.statement_id,
        merchant_key=txn.merchant_key,
        kind=txn.kind,
        issuer_category=txn.issuer_category,
        classified_by=txn.classified_by,
        category_id=txn.category_id,
        subcategory_id=txn.subcategory_id,
    )


def _safe_matches(rule: Rule, merchant_key: str) -> bool:
    """`rule_matches`, but a stored regex that no longer compiles matches nothing, so one bad rule
    cannot break the suggestions of every transaction (the cascade itself would raise)."""
    try:
        return rule_matches(rule, merchant_key)
    except (re.error, ConfigError):
        return False


def _chunks(values: Sequence[object]) -> Iterable[Sequence[object]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]


class CandidateResolver:
    """Computes `CandidateSet`s for many transactions with shared, memoized lookups.

    Create one per request/call (it snapshots the rules and the taxonomy); not thread-safe.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._category_keys: dict[int, str] = {
            row.id: row.key for row in session.execute(select(Category)).scalars()
        }
        self._subcategory_keys: dict[int, str] = {
            row.id: row.key for row in session.execute(select(Subcategory)).scalars()
        }
        self._user_rules, self._builtin_rules = cascade._load_rule_sets(session)
        self._maps: dict[str, MerchantMap | None] = {}
        self._parser_ids: dict[int, str | None] = {}
        self._user_rule_memo: dict[tuple[int, str], Rule | None] = {}
        self._builtin_memo: dict[tuple[int, str], Rule | None] = {}

    # ---- batched reads --------------------------------------------------------------------

    def prefetch(self, facts: Iterable[TxnFacts]) -> None:
        """Load the `merchant_map` rows and statement parser ids `facts` will need, in a few
        batched queries (payments and transfers never reach those layers, so they are skipped)."""
        need_keys: set[str] = set()
        need_statements: set[int] = set()
        for f in facts:
            if f.kind in _NON_SPEND_KINDS:
                continue
            if f.merchant_key and f.merchant_key not in self._maps:
                need_keys.add(f.merchant_key)
            if f.issuer_category and f.statement_id not in self._parser_ids:
                need_statements.add(f.statement_id)
        keys = sorted(need_keys)
        for chunk in _chunks(keys):
            rows = self._session.execute(
                select(MerchantMap).where(MerchantMap.merchant_key.in_(chunk))
            ).scalars()
            for row in rows:
                self._maps[row.merchant_key] = row
        for key in keys:
            self._maps.setdefault(key, None)
        ids = sorted(need_statements)
        for chunk in _chunks(ids):
            for statement_id, parser_id in self._session.execute(
                select(Statement.id, Statement.parser_id).where(Statement.id.in_(chunk))
            ):
                self._parser_ids[statement_id] = parser_id
        for statement_id in ids:
            self._parser_ids.setdefault(statement_id, None)

    def _map_row(self, merchant_key: str) -> MerchantMap | None:
        if not merchant_key:
            return None
        if merchant_key not in self._maps:
            self._maps[merchant_key] = self._session.get(MerchantMap, merchant_key)
        return self._maps[merchant_key]

    def _parser_id(self, statement_id: int) -> str | None:
        if statement_id not in self._parser_ids:
            statement = self._session.get(Statement, statement_id)
            self._parser_ids[statement_id] = statement.parser_id if statement else None
        return self._parser_ids[statement_id]

    # ---- layers ----------------------------------------------------------------------------

    def _pair(self, category_id: int | None, subcategory_id: int | None) -> tuple[str, str] | None:
        if category_id is None or subcategory_id is None:
            return None
        category_key = self._category_keys.get(category_id)
        subcategory_key = self._subcategory_keys.get(subcategory_id)
        if category_key is None or subcategory_key is None:
            return None
        return category_key, subcategory_key

    def _candidate(
        self,
        source: str,
        category_id: int | None,
        subcategory_id: int | None,
        *,
        label: str,
        kind: str | None = None,
        rule_id: int | None = None,
    ) -> Candidate | None:
        pair = self._pair(category_id, subcategory_id)
        if pair is None:
            return None
        return Candidate(
            source=source,
            category_key=pair[0],
            subcategory_key=pair[1],
            kind=kind,
            label=label,
            rule_id=rule_id,
            is_current=False,
        )

    def _rule_candidate(self, source: str, rule: Rule | None) -> Candidate | None:
        if rule is None:
            return None
        return self._candidate(
            source,
            rule.category_id,
            rule.subcategory_id,
            label=rule.pattern,
            kind=rule.kind_override,
            rule_id=rule.id,
        )

    def _layers(self, f: TxnFacts) -> list[Candidate]:
        """Every layer's candidate, in precedence order, mirroring
        `cascade._classify_deterministic` step by step (but never stopping at the first hit)."""
        if f.kind in _NON_SPEND_KINDS:
            kind_candidate = Candidate(
                source=SOURCE_KIND,
                category_key=cascade._PAYMENTS_TRANSFERS_CATEGORY,
                subcategory_key=cascade._PAYMENTS_TRANSFERS_SUBCATEGORY,
                kind=None,
                label=f"{f.kind} (never categorized further)",
                rule_id=None,
                is_current=False,
            )
            return [kind_candidate]

        found: list[Candidate | None] = []

        user_key = (f.user_id, f.merchant_key)
        if user_key not in self._user_rule_memo:
            self._user_rule_memo[user_key] = cascade.first_matching_rule(
                self._user_rules,
                user_id=f.user_id,
                merchant_key=f.merchant_key,
                matches=_safe_matches,
            )
        found.append(self._rule_candidate(SOURCE_USER_RULE, self._user_rule_memo[user_key]))

        row = self._map_row(f.merchant_key)
        if row is not None and row.source in cascade.USER_PIN_SOURCES:
            found.append(
                self._candidate(
                    SOURCE_USER_RULE,
                    row.category_id,
                    row.subcategory_id,
                    label=row.merchant_key,
                )
            )

        if user_key not in self._builtin_memo:
            self._builtin_memo[user_key] = cascade.first_matching_rule(
                self._builtin_rules,
                user_id=f.user_id,
                merchant_key=f.merchant_key,
                matches=_safe_matches,
            )
        found.append(self._rule_candidate(SOURCE_BUILTIN_RULE, self._builtin_memo[user_key]))

        if f.issuer_category:
            match = cascade.issuer_match(self._parser_id(f.statement_id), f.issuer_category)
            if match is not None:
                found.append(
                    Candidate(
                        source=SOURCE_ISSUER,
                        category_key=match.category_key,
                        subcategory_key=match.subcategory_key,
                        kind=None,
                        label=f.issuer_category,
                        rule_id=None,
                        is_current=False,
                    )
                )

        if row is not None and row.source in cascade.CACHE_SOURCES:
            from_llm = row.source == "llm"
            found.append(
                self._candidate(
                    SOURCE_LLM if from_llm else SOURCE_MERCHANT_CACHE,
                    row.category_id,
                    row.subcategory_id,
                    label="learned from LLM" if from_llm else "learned from issuer category",
                )
            )

        return [c for c in found if c is not None]

    # ---- public ----------------------------------------------------------------------------

    def resolve(self, f: TxnFacts) -> CandidateSet:
        layers = self._layers(f)
        edited = f.classified_by == USER_CLASSIFIED
        stored = self._pair(f.category_id, f.subcategory_id)

        if edited:
            edit = self._candidate(
                SOURCE_ONE_TIME_EDIT,
                f.category_id,
                f.subcategory_id,
                label="your one-time edit",
            )
            current = [replace(edit, is_current=True)] if edit is not None else []
            return CandidateSet(candidates=tuple(current + layers), conflict=False)

        marked: list[Candidate] = []
        found_current = False
        for candidate in layers:
            if not found_current and stored is not None and candidate.pair == stored:
                candidate = replace(candidate, is_current=True)
                found_current = True
            marked.append(candidate)
        distinct = {candidate.pair for candidate in marked}
        return CandidateSet(candidates=tuple(marked), conflict=len(distinct) >= 2)

    def conflict(self, f: TxnFacts) -> bool:
        """`resolve(f).conflict`, skipping the work for rows that can never conflict."""
        if f.classified_by == USER_CLASSIFIED or f.kind in _NON_SPEND_KINDS:
            return False
        return self.resolve(f).conflict


def candidates_for_transactions(
    session: Session, transactions: Sequence[Transaction]
) -> dict[int, CandidateSet]:
    """`CandidateSet` per transaction id, batch-computed (read-only; no LLM or network call)."""
    resolver = CandidateResolver(session)
    facts = [facts_of(t) for t in transactions]
    resolver.prefetch(facts)
    return {f.id: resolver.resolve(f) for f in facts}


def candidates_for_transaction(session: Session, transaction: Transaction) -> CandidateSet:
    return candidates_for_transactions(session, [transaction])[transaction.id]


def conflict_flags(session: Session, conditions: Sequence[ColumnElement[bool]]) -> dict[int, bool]:
    """`{transaction id: has a conflict}` for every transaction matching `conditions`.

    This is the engine behind `?conflict=true|false`: it reads only the nine columns candidate
    computation needs (not whole ORM rows), so a full-table scan stays cheap. Cost is one pass
    over the matching rows plus one rule scan per *distinct merchant* (see the module docstring);
    on a database with tens of thousands of transactions that is well under a second.
    """
    resolver = CandidateResolver(session)
    facts = [
        TxnFacts(*row) for row in session.execute(select(*_FACT_COLUMNS).where(*conditions)).all()
    ]
    resolver.prefetch(f for f in facts if f.classified_by != USER_CLASSIFIED)
    return {f.id: resolver.conflict(f) for f in facts}
