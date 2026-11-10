"""Tests for `classify/candidates.py`: what each cascade layer would say, and where they differ.

The layers are evaluated independently (no short-circuit) by reusing the cascade's own matching,
so the main guarantee tested here is the absence of drift: the first candidate is exactly what the
cascade would assign. No test here may reach an LLM (`FakeLLM` records zero requests).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.classify import candidates, cascade
from personal_finance_analyzer.classify.rules import sync_builtin_rules
from personal_finance_analyzer.db.models import (
    Account,
    MerchantMap,
    Rule,
    Statement,
    Transaction,
    User,
)
from tests.api.seed import add_txn, base, category_ids
from tests.llm_fakes import FakeLLM
from tests.llm_fakes import install as install_fake_llm

Seed = tuple[User, list[Account], Statement]

DINING = ("restaurant", "dine_in")
WHOLESALE = ("grocery", "wholesale_clubs")
GROCERY = ("grocery", "grocery_stores")
GAS = ("fuel", "gasoline")
FLIGHTS = ("travel", "flights")
UNCATEGORIZED = ("others", "uncategorized")


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """A fake LLM endpoint with nothing queued: any request fails the test loudly."""
    return install_fake_llm(monkeypatch)


def _rule(
    session: Session,
    seed: Seed,
    pattern: str,
    category: tuple[str, str],
    *,
    match_type: str = "contains",
    priority: int = 100,
    enabled: bool = True,
    kind_override: str | None = None,
    user_id: int | None = None,
) -> Rule:
    category_id, subcategory_id = category_ids(session, *category)
    rule = Rule(
        user_id=user_id if user_id is not None else seed[0].id,
        match_type=match_type,
        pattern=pattern,
        category_id=category_id,
        subcategory_id=subcategory_id,
        kind_override=kind_override,
        priority=priority,
        enabled=enabled,
        source="user",
    )
    session.add(rule)
    session.flush()
    return rule


def _map(
    session: Session, merchant_key: str, category: tuple[str, str], *, source: str
) -> MerchantMap:
    category_id, subcategory_id = category_ids(session, *category)
    row = MerchantMap(
        merchant_key=merchant_key,
        category_id=category_id,
        subcategory_id=subcategory_id,
        source=source,
        confidence=0.8,
        hit_count=0,
    )
    session.add(row)
    session.flush()
    return row


def _txn(
    session: Session, seed: Seed, index: int, merchant_key: str, **kwargs: object
) -> Transaction:
    txn = add_txn(session, seed, index, merchant_key=merchant_key, **kwargs)  # type: ignore[arg-type]
    session.flush()
    return txn


def _sources(candidate_set: candidates.CandidateSet) -> list[str]:
    return [c.source for c in candidate_set.candidates]


def test_each_layer_yields_its_own_candidate(session: Session, fake_llm: FakeLLM) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    rule = _rule(session, seed, "costco", DINING)
    _map(session, "costco", GAS, source="llm")
    txn = _txn(session, seed, 1, "costco whse", category=UNCATEGORIZED)
    txn.issuer_category = "AIRLINES"
    session.flush()

    result = candidates.candidates_for_transaction(session, txn)

    # The learned cache is keyed by the exact merchant_key, so a different key has none.
    assert _sources(result) == ["user_rule", "builtin_rule", "issuer"]
    user, builtin, issuer = result.candidates
    assert (user.category_key, user.subcategory_key) == DINING
    assert (user.label, user.rule_id, user.kind) == ("costco", rule.id, None)
    assert (builtin.category_key, builtin.subcategory_key) == WHOLESALE
    assert builtin.label == "COSTCO"
    assert (issuer.category_key, issuer.subcategory_key) == FLIGHTS
    assert issuer.label == "AIRLINES"
    assert result.conflict is False  # the user's own rule wins: they already decided
    assert fake_llm.requests == []


def test_cache_rows_become_llm_or_merchant_cache_candidates(session: Session) -> None:
    seed = base(session)
    _map(session, "zeta mart", GROCERY, source="llm")
    _map(session, "omega mart", GAS, source="issuer")
    llm_txn = _txn(session, seed, 1, "zeta mart")
    cache_txn = _txn(session, seed, 2, "omega mart")

    sets = candidates.candidates_for_transactions(session, [llm_txn, cache_txn])

    (llm,) = sets[llm_txn.id].candidates
    assert (llm.source, llm.category_key, llm.label) == ("llm", "grocery", "learned from LLM")
    (cache,) = sets[cache_txn.id].candidates
    assert (cache.source, cache.category_key) == ("merchant_cache", "fuel")
    assert "issuer" in cache.label


def test_legacy_user_pin_ranks_below_user_rules_and_above_builtin(session: Session) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    _map(session, "costco whse", GAS, source="user")
    pinned = _txn(session, seed, 1, "costco whse")
    _rule(session, seed, "costco", DINING)

    result = candidates.candidates_for_transaction(session, pinned)

    assert _sources(result) == ["user_rule", "user_rule", "builtin_rule"]
    assert [c.category_key for c in result.candidates] == ["restaurant", "fuel", "grocery"]


def test_a_winning_user_rule_or_pin_settles_the_row_but_other_layers_still_conflict(
    session: Session,
) -> None:
    """Regression: old LLM merchant-map rows and overridden built-ins kept rows the user had
    settled with a rule flagged as conflicting."""
    seed = base(session)
    sync_builtin_rules(session)
    _rule(session, seed, "costco", DINING)
    _map(session, "costco whse", GAS, source="llm")
    ruled = _txn(session, seed, 1, "costco whse")
    pinned = _txn(session, seed, 2, "costco pin")
    _map(session, "costco pin", GAS, source="user")
    unruled = _txn(session, seed, 3, "costco other")
    session.flush()
    session.query(Rule).filter(Rule.source == "user").delete()  # unruled: builtin vs the LLM map
    _map(session, "costco other", GAS, source="llm")
    session.flush()

    sets = candidates.candidates_for_transactions(session, [ruled, pinned, unruled])

    assert sets[unruled.id].conflict is True  # builtin wins, the LLM cache disagrees
    # re-create the user rule: now the same row is settled
    _rule(session, seed, "costco", DINING)
    sets = candidates.candidates_for_transactions(session, [ruled, pinned, unruled])
    assert sets[ruled.id].conflict is False
    assert sets[pinned.id].conflict is False
    assert sets[unruled.id].conflict is False
    assert len(sets[ruled.id].candidates) >= 2  # the chips are still offered
    flags = candidates.conflict_flags(session, [Transaction.id > 0])
    assert not any(flags[i] for i in (ruled.id, pinned.id, unruled.id))


def test_conflict_needs_two_distinct_categories(session: Session) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    _rule(session, seed, "costco", WHOLESALE)
    agreeing = _txn(session, seed, 1, "costco whse")
    lonely = _txn(session, seed, 2, "starbucks")
    none = _txn(session, seed, 3, "zzz nobody knows")

    sets = candidates.candidates_for_transactions(session, [agreeing, lonely, none])

    assert len(sets[agreeing.id].candidates) == 2
    assert sets[agreeing.id].conflict is False  # same pair twice is not a conflict
    assert sets[lonely.id].conflict is False
    assert sets[none.id] == candidates.CandidateSet(candidates=(), conflict=False)


def test_current_is_the_highest_layer_matching_what_the_row_is_filed_under(
    session: Session,
) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    _rule(session, seed, "costco", DINING)
    winner_filed = _txn(session, seed, 1, "costco whse", category=DINING, classified_by="user_rule")
    stale = _txn(session, seed, 2, "costco whse", category=UNCATEGORIZED, classified_by="llm")
    lower_filed = _txn(session, seed, 3, "costco whse", category=WHOLESALE, classified_by="x")

    sets = candidates.candidates_for_transactions(session, [winner_filed, stale, lower_filed])

    assert [c.is_current for c in sets[winner_filed.id].candidates] == [True, False]
    assert [c.is_current for c in sets[stale.id].candidates] == [False, False]
    assert [c.is_current for c in sets[lower_filed.id].candidates] == [False, True]


def test_one_time_edit_is_current_still_lists_layers_and_is_never_a_conflict(
    session: Session,
) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    _rule(session, seed, "costco", DINING)
    edited = _txn(session, seed, 1, "costco whse", category=GAS, classified_by="user")

    result = candidates.candidates_for_transaction(session, edited)

    assert _sources(result) == ["one_time_edit", "user_rule", "builtin_rule"]
    edit, user, builtin = result.candidates
    assert edit.is_current and (edit.category_key, edit.subcategory_key) == GAS
    assert not user.is_current and not builtin.is_current
    assert result.conflict is False


def test_payments_and_transfers_only_have_the_fixed_kind_candidate(session: Session) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    _rule(session, seed, "costco", DINING)
    payment = _txn(
        session, seed, 1, "costco whse", kind="payment", category=("others", "payments_transfers")
    )

    result = candidates.candidates_for_transaction(session, payment)

    (only,) = result.candidates
    assert only.source == "kind" and only.is_current
    assert (only.category_key, only.subcategory_key) == ("others", "payments_transfers")
    assert result.conflict is False


def test_disabled_foreign_and_broken_rules_are_ignored(session: Session) -> None:
    seed = base(session)
    stranger = User(name="Stranger")
    session.add(stranger)
    session.flush()
    _rule(session, seed, "acme", DINING, enabled=False)
    _rule(session, seed, "acme", FLIGHTS, user_id=stranger.id)
    broken = _rule(session, seed, "(", DINING, match_type="regex")
    _rule(session, seed, "acme", GAS, priority=200)
    txn = _txn(session, seed, 1, "acme store")

    result = candidates.candidates_for_transaction(session, txn)

    # Only the enabled rule of this user shows up; an uncompilable regex is skipped rather than
    # failing the whole computation (the cascade itself would raise on it).
    assert broken.id not in [c.rule_id for c in result.candidates]
    assert [c.category_key for c in result.candidates] == ["fuel"]


def test_rule_kind_override_travels_with_the_candidate(session: Session) -> None:
    seed = base(session)
    _rule(session, seed, "refundco", DINING, kind_override="refund")
    txn = _txn(session, seed, 1, "refundco")

    (candidate,) = candidates.candidates_for_transaction(session, txn).candidates

    assert candidate.kind == "refund"


def test_computation_is_read_only_and_never_calls_the_llm(
    session: Session, fake_llm: FakeLLM
) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    pin = _map(session, "pinned", GAS, source="user")
    cache = _map(session, "cached", GAS, source="llm")
    txns = [
        _txn(session, seed, 1, "pinned"),
        _txn(session, seed, 2, "cached"),
        _txn(session, seed, 3, "never seen before"),
    ]
    txns[2].issuer_category = "DINING"
    session.flush()
    before = (pin.hit_count, cache.hit_count, session.dirty.copy(), len(session.new))

    candidates.candidates_for_transactions(session, txns)
    candidates.conflict_flags(session, [Transaction.id > 0])

    assert (pin.hit_count, cache.hit_count, session.dirty, len(session.new)) == before
    assert session.execute(select(MerchantMap)).scalars().all() == [pin, cache]
    assert fake_llm.requests == []


def test_rules_are_matched_once_per_distinct_merchant_not_once_per_row(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    _rule(session, seed, "will not match", DINING)
    txns = [_txn(session, seed, i, "zzz unmatched merchant") for i in range(1, 41)]
    rule_count = len(session.execute(select(Rule)).scalars().all())
    calls = 0
    real = candidates.rule_matches

    def counting(rule: Rule, merchant_key: str) -> bool:
        nonlocal calls
        calls += 1
        return real(rule, merchant_key)

    monkeypatch.setattr(candidates, "rule_matches", counting)

    candidates.candidates_for_transactions(session, txns)
    assert calls == rule_count  # 40 rows, one merchant: each rule evaluated once

    calls = 0
    candidates.conflict_flags(session, [Transaction.id > 0])
    assert calls == rule_count


# --------------------------------------------------------------------------------------------
# No drift: the first candidate is what the cascade would assign.
# --------------------------------------------------------------------------------------------


def _scenario_txns(session: Session, seed: Seed) -> list[Transaction]:
    """One transaction per cascade outcome (and one per overlap) for the drift test."""
    sync_builtin_rules(session)
    _rule(session, seed, "mine", DINING, kind_override="refund")
    _rule(session, seed, "both", GAS)
    _map(session, "pinned", FLIGHTS, source="user")
    _map(session, "learned llm", GROCERY, source="llm")
    _map(session, "learned issuer", GAS, source="issuer")
    _map(session, "both", FLIGHTS, source="llm")
    rows = [
        _txn(session, seed, 1, "mine shop"),  # user rule
        _txn(session, seed, 2, "pinned"),  # legacy pin
        _txn(session, seed, 3, "costco whse"),  # builtin rule
        _txn(session, seed, 4, "unknown one"),  # issuer label
        _txn(session, seed, 5, "learned llm"),  # llm cache
        _txn(session, seed, 6, "learned issuer"),  # issuer-learned cache
        _txn(session, seed, 7, "both"),  # user rule beats llm cache
        _txn(session, seed, 8, "starbucks"),  # builtin; issuer says otherwise below
        _txn(session, seed, 9, "nothing matches"),  # LLM would be needed
    ]
    rows[3].issuer_category = "GROCERIES"
    rows[7].issuer_category = "AIRLINES"
    session.flush()
    return rows


def test_first_candidate_equals_what_the_cascade_assigns(session: Session) -> None:
    seed = base(session)
    rows = _scenario_txns(session, seed)
    user_rules, builtin_rules = cascade._load_rule_sets(session)
    sets = candidates.candidates_for_transactions(session, rows)

    seen_sources: set[str] = set()
    for txn in rows:
        outcome = cascade._classify_deterministic(
            session, txn, user_rules=user_rules, builtin_rules=builtin_rules, dry_run=True
        )
        result = sets[txn.id]
        if outcome is None:
            assert result.candidates == (), txn.merchant_key  # only the LLM could answer
            continue
        first = result.candidates[0]
        assert (first.category_key, first.subcategory_key) == (
            outcome.category_key,
            outcome.subcategory_key,
        ), txn.merchant_key
        assert first.kind == outcome.kind_override, txn.merchant_key
        seen_sources.add(first.source)

    assert seen_sources == {
        "user_rule",
        "builtin_rule",
        "issuer",
        "llm",
        "merchant_cache",
    }


def test_first_candidate_equals_the_cascade_for_payments(session: Session) -> None:
    seed = base(session)
    payment = _txn(session, seed, 1, "some payment", kind="payment")
    user_rules, builtin_rules = cascade._load_rule_sets(session)
    outcome = cascade._classify_deterministic(
        session, payment, user_rules=user_rules, builtin_rules=builtin_rules, dry_run=True
    )
    assert outcome is not None

    (first,) = candidates.candidates_for_transaction(session, payment).candidates

    assert (first.category_key, first.subcategory_key) == (
        outcome.category_key,
        outcome.subcategory_key,
    )


def test_conflict_flags_agree_with_per_row_resolution(session: Session) -> None:
    seed = base(session)
    rows = _scenario_txns(session, seed)
    edited = _txn(session, seed, 20, "mine shop", category=GAS, classified_by="user")

    flags = candidates.conflict_flags(session, [Transaction.id > 0])
    sets = candidates.candidates_for_transactions(session, [*rows, edited])

    assert flags == {txn_id: result.conflict for txn_id, result in sets.items()}
    assert flags[edited.id] is False
    assert any(flags.values()) and not all(flags.values())
