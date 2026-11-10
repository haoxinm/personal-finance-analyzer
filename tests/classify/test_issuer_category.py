"""Tests for `builtin_rules.yaml` / `issuer_category_map.yaml` loading and `classify/rules.py`
(D8, A16, §2b P2-B)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.classify import rules
from personal_finance_analyzer.db.models import Rule


def test_builtin_rules_all_validate_and_self_test() -> None:
    # Importing `personal_finance_analyzer.classify.rules` already ran `load_builtin_rules()` at module
    # import time (it would have raised `ConfigError` on any bad entry); re-running it here
    # exercises the same validation deterministically within this test.
    reloaded = rules.load_builtin_rules()
    assert reloaded == rules.BUILTIN_RULES
    assert len(reloaded) >= 5  # a small, high-precision starter set (D8) -- not empty


def test_builtin_rules_have_no_duplicate_pattern_match_type_pairs() -> None:
    seen = {(r.pattern, r.match_type) for r in rules.BUILTIN_RULES}
    assert len(seen) == len(rules.BUILTIN_RULES)


def test_sync_builtin_rules_is_idempotent(session: Session) -> None:
    rules.sync_builtin_rules(session)
    session.commit()
    before = session.execute(select(Rule.id).where(Rule.source == "builtin")).all()

    rules.sync_builtin_rules(session)
    session.commit()
    after = session.execute(select(Rule.id).where(Rule.source == "builtin")).all()

    assert len(before) == len(after) == len(rules.BUILTIN_RULES)


def test_sync_builtin_rules_never_touches_a_user_rule(session: Session) -> None:
    from personal_finance_analyzer.db.models import Category, Subcategory

    category = session.execute(select(Category).where(Category.key == "others")).scalar_one()
    subcategory = session.execute(
        select(Subcategory).where(
            Subcategory.category_id == category.id, Subcategory.key == "uncategorized"
        )
    ).scalar_one()
    user_rule = Rule(
        user_id=None,
        match_type="exact",
        pattern="some merchant",
        category_id=category.id,
        subcategory_id=subcategory.id,
        source="user",
        priority=50,
        enabled=True,
    )
    session.add(user_rule)
    session.commit()

    rules.sync_builtin_rules(session)
    session.commit()

    refreshed = session.get(Rule, user_rule.id)
    assert refreshed is not None
    assert refreshed.source == "user"
    assert refreshed.pattern == "some merchant"


def test_rule_matches_contains_and_regex_and_exact() -> None:
    assert rules.rule_matches_text("contains", "STARBUCKS", "starbucks store seattle wa")
    assert not rules.rule_matches_text("contains", "STARBUCKS", "peets coffee seattle wa")
    assert rules.rule_matches_text("regex", r"^SHELL\b", "shell oil seattle wa")
    assert rules.rule_matches_text("exact", "costco", "Costco")
    assert not rules.rule_matches_text("exact", "costco", "costco warehouse")


def test_builtin_rule_starbucks_resolves_dine_in() -> None:
    matching = [r for r in rules.BUILTIN_RULES if r.pattern == "STARBUCKS"]
    assert len(matching) == 1
    assert matching[0].category_key == "restaurant"
    assert matching[0].subcategory_key == "dine_in"


def test_resolve_issuer_category_unambiguous_hit() -> None:
    match = rules.resolve_issuer_category("layout_c_credit", "DINING")
    assert match is not None
    assert match.category_key == "restaurant"
    assert match.subcategory_key == "dine_in"


def test_resolve_issuer_category_is_case_and_whitespace_insensitive() -> None:
    match = rules.resolve_issuer_category("layout_c_credit", "  dining  ")
    assert match is not None
    assert match.subcategory_key == "dine_in"


def test_resolve_issuer_category_falls_back_to_wildcard_parser() -> None:
    match = rules.resolve_issuer_category("some_future_parser", "GROCERIES")
    assert match is not None
    assert match.category_key == "grocery"


def test_resolve_issuer_category_ambiguous_label_is_absent() -> None:
    # A16: "MERCHANDISE"/"SERVICES" are deliberately absent -- they fall through to the LLM.
    assert rules.resolve_issuer_category("layout_c_credit", "MERCHANDISE") is None
    assert rules.resolve_issuer_category("layout_c_credit", "SERVICES") is None


def test_resolve_issuer_category_unknown_label_returns_none() -> None:
    assert rules.resolve_issuer_category("layout_c_credit", "SOME UNKNOWN LABEL") is None


def test_resolve_issuer_category_empty_label_returns_none() -> None:
    assert rules.resolve_issuer_category("layout_c_credit", None) is None
    assert rules.resolve_issuer_category("layout_c_credit", "") is None
