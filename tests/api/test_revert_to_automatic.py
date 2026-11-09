"""Handing a one-time-edited row back to automatic classification (`revert-to-automatic`).

A one-time edit is final for every automated pass, so without this action a row you once clicked
could never follow a rule again. Reverting drops the edit and re-runs the *deterministic* cascade
for that row; it must never reach the LLM, and its undo token must put the edit back exactly.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from personal_finance_analyzer.api.schemas import TransactionState
from personal_finance_analyzer.classify.rules import sync_builtin_rules
from personal_finance_analyzer.core.types import CLASSIFIED_BY_VALUES
from personal_finance_analyzer.db.models import Classification, MerchantMap, Rule, Transaction
from tests.api.seed import add_txn, base, category_ids
from tests.llm_fakes import FakeLLM
from tests.llm_fakes import install as install_fake_llm

DINING = ("restaurant", "dine_in")
WHOLESALE = ("grocery", "wholesale_clubs")
GAS = ("fuel", "gasoline")
UNCATEGORIZED = ("others", "uncategorized")


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """A fake LLM with nothing queued: any call would fail the test (see `requests` checks)."""
    return install_fake_llm(monkeypatch)


def _user_rule(session: Session, user_id: int, pattern: str, category: tuple[str, str]) -> Rule:
    category_id, subcategory_id = category_ids(session, *category)
    rule = Rule(
        user_id=user_id,
        match_type="contains",
        pattern=pattern,
        category_id=category_id,
        subcategory_id=subcategory_id,
        priority=100,
        enabled=True,
        source="user",
    )
    session.add(rule)
    return rule


def _pair(session: Session, txn: Transaction) -> tuple[str, str] | None:
    session.expire_all()
    fresh = session.get(Transaction, txn.id)
    assert fresh is not None
    if fresh.category_id is None or fresh.subcategory_id is None:
        return None
    for key in (DINING, WHOLESALE, GAS, UNCATEGORIZED):
        if category_ids(session, *key) == (fresh.category_id, fresh.subcategory_id):
            return key
    raise AssertionError("unexpected category")


def _edited_costco(session: Session) -> tuple[Transaction, Rule]:
    """A Costco row the user edited to GAS, with a user rule (dining) that now wins automatically."""
    seed = base(session)
    sync_builtin_rules(session)
    rule = _user_rule(session, seed[0].id, "costco", DINING)
    txn = add_txn(session, seed, 1, merchant_key="costco whse", category=GAS, classified_by="user")
    session.commit()
    return txn, rule


def test_revert_clears_the_edit_and_reruns_the_cascade(
    client: TestClient, session: Session, fake_llm: FakeLLM
) -> None:
    txn, _ = _edited_costco(session)

    response = client.post(f"/api/transactions/{txn.id}/revert-to-automatic")

    assert response.status_code == 200, response.text
    assert response.json()["transaction"]["subcategory_key"] == "dine_in"
    session.expire_all()
    stored = session.get(Transaction, txn.id)
    assert stored is not None
    assert stored.classified_by == "user_rule"
    assert stored.needs_review is False
    assert _pair(session, txn) == DINING
    assert fake_llm.requests == []


def test_a_reverted_row_follows_later_rule_changes_again(
    client: TestClient, session: Session, fake_llm: FakeLLM
) -> None:
    """The point of S11: after reverting, a rule change reaches the row (it never could while the
    row carried `classified_by='user'`)."""
    txn, rule = _edited_costco(session)
    client.post(f"/api/transactions/{txn.id}/revert-to-automatic")

    category_id, subcategory_id = category_ids(session, *WHOLESALE)
    stored_rule = session.get(Rule, rule.id)
    assert stored_rule is not None
    stored_rule.category_id, stored_rule.subcategory_id = category_id, subcategory_id
    session.commit()
    applied = client.post(f"/api/rules/{rule.id}/apply")

    assert applied.status_code == 200, applied.text
    assert _pair(session, txn) == WHOLESALE
    assert fake_llm.requests == []


def test_revert_with_no_deterministic_answer_is_uncategorized_and_needs_review(
    client: TestClient, session: Session, fake_llm: FakeLLM
) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    txn = add_txn(session, seed, 1, merchant_key="nobody knows", category=GAS, classified_by="user")
    session.commit()

    response = client.post(f"/api/transactions/{txn.id}/revert-to-automatic")

    assert response.status_code == 200, response.text
    session.expire_all()
    stored = session.get(Transaction, txn.id)
    assert stored is not None
    assert stored.classified_by == "llm"  # what the import path writes for an unplaced row
    assert stored.needs_review is True
    assert _pair(session, txn) == UNCATEGORIZED
    # The model is only ever asked through an explicit re-classify, never here.
    assert fake_llm.requests == []


def test_revert_uses_the_saved_merchant_cache_answer_without_calling_the_llm(
    client: TestClient, session: Session, fake_llm: FakeLLM
) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    category_id, subcategory_id = category_ids(session, *WHOLESALE)
    session.add(
        MerchantMap(
            merchant_key="saved merchant",
            merchant_canonical="Saved",
            category_id=category_id,
            subcategory_id=subcategory_id,
            source="llm",
            confidence=0.95,
            hit_count=0,
            updated_at="2026-01-01T00:00:00Z",
        )
    )
    txn = add_txn(
        session, seed, 1, merchant_key="saved merchant", category=GAS, classified_by="user"
    )
    session.commit()

    response = client.post(f"/api/transactions/{txn.id}/revert-to-automatic")

    assert response.status_code == 200, response.text
    session.expire_all()
    stored = session.get(Transaction, txn.id)
    assert stored is not None
    assert (stored.classified_by, stored.needs_review) == ("cache", False)
    assert _pair(session, txn) == WHOLESALE
    cache = session.get(MerchantMap, "saved merchant")
    assert cache is not None and cache.hit_count == 0  # a revert is not a cache hit
    assert fake_llm.requests == []


def test_undo_token_restores_the_one_time_edit(
    client: TestClient, session: Session, fake_llm: FakeLLM
) -> None:
    txn, _ = _edited_costco(session)
    reverted = client.post(f"/api/transactions/{txn.id}/revert-to-automatic").json()
    assert _pair(session, txn) == DINING

    undone = client.post("/api/review/undo", json=reverted["undo"])

    assert undone.status_code == 200, undone.text
    assert undone.json() == {"restored": 1, "skipped": [], "rule_skipped": False}
    session.expire_all()
    restored = session.get(Transaction, txn.id)
    assert restored is not None
    assert (restored.classified_by, restored.needs_review) == ("user", False)
    assert _pair(session, txn) == GAS
    assert fake_llm.requests == []


def test_undo_does_not_overwrite_an_edit_made_after_the_revert(
    client: TestClient, session: Session, fake_llm: FakeLLM
) -> None:
    """Undo restores a row only if it still equals what the revert left: a row the user edited
    again in between is reported as skipped and keeps that newer edit."""
    seed = base(session)
    sync_builtin_rules(session)
    _user_rule(session, seed[0].id, "costco", DINING)
    one = add_txn(session, seed, 1, merchant_key="costco a", category=GAS, classified_by="user")
    two = add_txn(session, seed, 2, merchant_key="costco b", category=GAS, classified_by="user")
    session.commit()
    reverted = client.post(
        "/api/transactions/bulk-revert-to-automatic",
        json={"transaction_ids": [one.id, two.id]},
    ).json()
    assert _pair(session, one) == DINING and _pair(session, two) == DINING

    # The user edits the first row again before pressing Undo.
    patched = client.patch(
        f"/api/transactions/{one.id}",
        json={"category_key": WHOLESALE[0], "subcategory_key": WHOLESALE[1]},
    )
    assert patched.status_code == 200, patched.text

    undone = client.post("/api/review/undo", json=reverted["undo"])

    assert undone.status_code == 200, undone.text
    assert undone.json() == {"restored": 1, "skipped": [one.id], "rule_skipped": False}
    assert _pair(session, one) == WHOLESALE  # the newer edit survives
    assert _pair(session, two) == GAS  # the untouched row went back to its one-time edit
    stored = session.get(Transaction, two.id)
    assert stored is not None and stored.classified_by == "user"
    assert fake_llm.requests == []


def test_candidates_mark_the_automatic_answer_for_an_edited_row(
    client: TestClient, session: Session
) -> None:
    txn, _ = _edited_costco(session)

    candidates = client.get(f"/api/transactions/{txn.id}/candidates").json()["candidates"]

    assert [(c["source"], c["is_current"], c["is_automatic"]) for c in candidates] == [
        ("one_time_edit", True, False),
        ("user_rule", False, True),
        ("builtin_rule", False, False),
    ]
    reverted = client.post(f"/api/transactions/{txn.id}/revert-to-automatic")
    assert reverted.status_code == 200
    after = client.get(f"/api/transactions/{txn.id}/candidates").json()["candidates"]
    assert [(c["source"], c["is_current"], c["is_automatic"]) for c in after] == [
        ("user_rule", True, True),
        ("builtin_rule", False, False),
    ]


def test_revert_of_an_automatic_row_is_a_conflict_and_changes_nothing(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    txn = add_txn(session, seed, 1, merchant_key="x", category=DINING, classified_by="builtin_rule")
    session.commit()

    response = client.post(f"/api/transactions/{txn.id}/revert-to-automatic")

    assert response.status_code == 409
    assert _pair(session, txn) == DINING
    assert client.post("/api/transactions/99999/revert-to-automatic").status_code == 404


def test_bulk_revert_skips_unedited_and_missing_rows_and_undoes_as_one(
    client: TestClient, session: Session, fake_llm: FakeLLM
) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    _user_rule(session, seed[0].id, "costco", DINING)
    one = add_txn(session, seed, 1, merchant_key="costco a", category=GAS, classified_by="user")
    two = add_txn(session, seed, 2, merchant_key="costco b", category=GAS, classified_by="user")
    auto = add_txn(
        session, seed, 3, merchant_key="costco c", category=DINING, classified_by="user_rule"
    )
    session.commit()

    response = client.post(
        "/api/transactions/bulk-revert-to-automatic",
        json={"transaction_ids": [one.id, two.id, auto.id, 99999]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["reverted"], body["skipped"]) == (2, 2)
    assert _pair(session, one) == DINING and _pair(session, two) == DINING
    audit = session.query(Classification).filter(Classification.transaction_id == auto.id).count()
    assert audit == 0  # the unedited row was not rewritten

    undone = client.post("/api/review/undo", json=body["undo"])
    assert undone.json() == {"restored": 2, "skipped": [], "rule_skipped": False}
    assert _pair(session, one) == GAS and _pair(session, two) == GAS
    stored = session.get(Transaction, one.id)
    assert stored is not None and stored.classified_by == "user"
    assert fake_llm.requests == []


def test_revert_applies_a_rules_kind_override_and_undo_restores_the_kind(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    rule = _user_rule(session, seed[0].id, "refundco", DINING)
    rule.kind_override = "refund"
    txn = add_txn(session, seed, 1, merchant_key="refundco 1", category=GAS, classified_by="user")
    session.commit()

    reverted = client.post(f"/api/transactions/{txn.id}/revert-to-automatic").json()

    session.expire_all()
    stored = session.get(Transaction, txn.id)
    assert stored is not None and stored.kind == "refund" and not stored.is_spend

    client.post("/api/review/undo", json=reverted["undo"])
    session.expire_all()
    stored = session.get(Transaction, txn.id)
    assert stored is not None and stored.kind == "purchase" and stored.is_spend


def test_undo_round_trips_a_revert_that_found_no_deterministic_answer(
    client: TestClient, session: Session, fake_llm: FakeLLM
) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    txn = add_txn(session, seed, 1, merchant_key="nobody knows", category=GAS, classified_by="user")
    session.commit()
    reverted = client.post(f"/api/transactions/{txn.id}/revert-to-automatic")
    assert reverted.status_code == 200, reverted.text
    assert _pair(session, txn) == UNCATEGORIZED

    undone = client.post("/api/review/undo", json=reverted.json()["undo"])

    assert undone.status_code == 200, undone.text
    session.expire_all()
    restored = session.get(Transaction, txn.id)
    assert restored is not None
    assert (restored.classified_by, restored.needs_review) == ("user", False)
    assert _pair(session, txn) == GAS
    assert fake_llm.requests == []


def test_api_classified_by_type_matches_the_core_value_set() -> None:
    schema = TransactionState.model_json_schema()["properties"]["classified_by"]
    enum_values = {v for branch in schema["anyOf"] for v in branch.get("enum", [])}
    assert enum_values == CLASSIFIED_BY_VALUES
