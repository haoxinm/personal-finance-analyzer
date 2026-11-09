"""Review accept/undo, applying a rule to existing rows, and what a re-classify run covers."""

from __future__ import annotations

import time
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from spend_analyzer.api.services import gateway
from spend_analyzer.db.models import MerchantMap, Rule, Transaction
from tests.api.seed import add_txn, base, category_ids

GROCERY = ("grocery", "grocery_stores")
UNCATEGORIZED = ("others", "uncategorized")


def _accept(
    client: TestClient,
    txn_id: int,
    *,
    create_rule: bool = False,
    similar_ids: Sequence[int] = (),
    category: tuple[str, str] = GROCERY,
) -> dict[str, object]:
    response = client.post(
        "/api/review/accept",
        json={
            "transaction_id": txn_id,
            "category_key": category[0],
            "subcategory_key": category[1],
            "create_rule": create_rule,
            "similar_ids": list(similar_ids),
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def _pending(session: Session, index: int, seed: object, **kwargs: object) -> Transaction:
    txn = add_txn(
        session,
        seed,  # type: ignore[arg-type]
        index,
        category=UNCATEGORIZED,
        needs_review=True,
        classified_by="llm",
        **kwargs,  # type: ignore[arg-type]
    )
    return txn


def test_accept_applies_to_similar_pending_rows_of_the_same_merchant_only(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    first = _pending(session, 1, seed, merchant_key="trader joes")
    same_a = _pending(session, 2, seed, merchant_key="trader joes")
    same_b = _pending(session, 3, seed, merchant_key="trader joes")
    other_merchant = _pending(session, 4, seed, merchant_key="shell")
    already_done = add_txn(
        session, seed, 5, merchant_key="trader joes", category=GROCERY, classified_by="user"
    )
    session.commit()

    body = _accept(
        client,
        first.id,
        similar_ids=[same_a.id, same_b.id, other_merchant.id, already_done.id, first.id, 999_999],
    )
    # Only the two genuine same-merchant pending rows were applied; the rest were refused.
    assert sorted(body["similar_ids"]) == sorted([same_a.id, same_b.id])  # type: ignore[arg-type]

    session.expire_all()
    grocery_id, grocery_sub = category_ids(session, *GROCERY)
    for txn_id in (first.id, same_a.id, same_b.id):
        txn = session.get(Transaction, txn_id)
        assert txn is not None
        assert (txn.category_id, txn.subcategory_id) == (grocery_id, grocery_sub)
        assert txn.needs_review is False
        assert txn.classified_by == "user"
    untouched = session.get(Transaction, other_merchant.id)
    assert untouched is not None and untouched.needs_review is True


def test_undo_restores_rows_needs_review_and_removes_the_rule_it_created(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    first = _pending(session, 1, seed, merchant_key="trader joes")
    second = _pending(session, 2, seed, merchant_key="trader joes")
    session.commit()
    uncategorized = category_ids(session, *UNCATEGORIZED)

    body = _accept(client, first.id, create_rule=True, similar_ids=[second.id])
    session.expire_all()
    assert session.execute(select(Rule).where(Rule.pattern == "trader joes")).scalars().all()

    undone = client.post("/api/review/undo", json=body["undo"])
    assert undone.status_code == 200
    assert undone.json()["restored"] == 2

    session.expire_all()
    for txn_id in (first.id, second.id):
        txn = session.get(Transaction, txn_id)
        assert txn is not None
        assert (txn.category_id, txn.subcategory_id) == uncategorized
        assert txn.needs_review is True
        assert txn.classified_by == "llm"
    assert session.execute(select(Rule).where(Rule.pattern == "trader joes")).scalars().all() == []


def test_undo_restores_a_transaction_that_had_no_category(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    bare = add_txn(session, seed, 1, merchant_key="mystery", needs_review=True)
    session.commit()
    assert bare.category_id is None

    body = _accept(client, bare.id)
    assert client.post("/api/review/undo", json=body["undo"]).json()["restored"] == 1

    session.expire_all()
    txn = session.get(Transaction, bare.id)
    assert txn is not None
    assert txn.category_id is None and txn.subcategory_id is None
    assert txn.needs_review is True


def test_undo_puts_back_the_previous_version_of_an_existing_rule(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    grocery = category_ids(session, *GROCERY)
    first = _pending(session, 1, seed, merchant_key="trader joes")
    session.commit()
    fuel = category_ids(session, "fuel", "gasoline")

    # "Always categorize" twice: the second updates the one exact rule, not a duplicate.
    _accept(client, first.id, create_rule=True)
    again = _accept(client, first.id, create_rule=True, category=("fuel", "gasoline"))
    session.expire_all()
    rules = session.execute(select(Rule).where(Rule.pattern == "trader joes")).scalars().all()
    assert len(rules) == 1
    assert (rules[0].category_id, rules[0].subcategory_id) == fuel

    client.post("/api/review/undo", json=again["undo"])
    session.expire_all()
    rule = session.execute(select(Rule).where(Rule.pattern == "trader joes")).scalar_one()
    assert (rule.category_id, rule.subcategory_id) == grocery  # back to the first accept's rule
    assert session.get(MerchantMap, "trader joes") is None  # accepting never writes a pin


def test_accept_rejects_unknown_category_and_missing_transaction(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    txn = _pending(session, 1, seed)
    session.commit()
    bad = client.post(
        "/api/review/accept",
        json={"transaction_id": txn.id, "category_key": "nope", "subcategory_key": "x"},
    )
    assert bad.status_code == 400
    missing = client.post(
        "/api/review/accept",
        json={"transaction_id": 999_999, "category_key": "grocery", "subcategory_key": "x"},
    )
    assert missing.status_code == 404


# --- Rules apply retroactively --------------------------------------------------------------


def _create_rule(client: TestClient, pattern: str, **extra: object) -> dict[str, object]:
    response = client.post(
        "/api/rules",
        json={
            "pattern": pattern,
            "match_type": "contains",
            "category_key": "grocery",
            "subcategory_key": "grocery_stores",
            **extra,
        },
    )
    assert response.status_code == 201
    body: dict[str, object] = response.json()
    return body


def test_new_rule_reports_and_applies_to_existing_matches_but_never_user_corrections(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    pending = _pending(session, 1, seed, merchant_key="trader joes 123")
    uncat = _pending(session, 2, seed, merchant_key="trader joes market")
    corrected = add_txn(
        session,
        seed,
        3,
        merchant_key="trader joes east",
        category=("fuel", "gasoline"),
        classified_by="user",
    )
    unrelated = _pending(session, 4, seed, merchant_key="shell")
    paid = add_txn(session, seed, 5, merchant_key="trader joes card", kind="payment")
    session.commit()

    rule = _create_rule(client, "trader joes")
    assert rule["existing_matches"] == 2  # not the corrected row, the payment, or shell
    assert rule["match_type"] == "contains" and rule["priority"] == 100

    # Creating alone changes nothing; applying does.
    session.expire_all()
    assert session.get(Transaction, pending.id).needs_review is True  # type: ignore[union-attr]

    applied = client.post(f"/api/rules/{rule['id']}/apply")
    assert applied.status_code == 200
    assert applied.json()["updated"] == 2

    session.expire_all()
    grocery = category_ids(session, *GROCERY)
    for txn_id in (pending.id, uncat.id):
        txn = session.get(Transaction, txn_id)
        assert txn is not None
        assert (txn.category_id, txn.subcategory_id) == grocery
        assert txn.classified_by == "user_rule" and txn.needs_review is False
    fuel = category_ids(session, "fuel", "gasoline")
    kept = session.get(Transaction, corrected.id)
    assert kept is not None and (kept.category_id, kept.subcategory_id) == fuel
    assert kept.classified_by == "user"
    other = session.get(Transaction, unrelated.id)
    assert other is not None and other.needs_review is True
    skipped = session.get(Transaction, paid.id)
    assert skipped is not None and skipped.category_id is None

    # Idempotent: nothing left to change.
    assert client.post(f"/api/rules/{rule['id']}/apply").json()["updated"] == 0


def test_applying_a_rule_respects_a_higher_priority_rule(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    txn = _pending(session, 1, seed, merchant_key="trader joes")
    session.commit()
    # An earlier rule (same priority, lower id) already claims this merchant.
    first = client.post(
        "/api/rules",
        json={
            "pattern": "joes",
            "category_key": "fuel",
            "subcategory_key": "gasoline",
        },
    ).json()
    second = _create_rule(client, "trader")
    assert second["existing_matches"] == 0

    assert client.post(f"/api/rules/{second['id']}/apply").json()["updated"] == 0
    assert client.post(f"/api/rules/{first['id']}/apply").json()["updated"] == 1
    session.expire_all()
    fuel = category_ids(session, "fuel", "gasoline")
    row = session.get(Transaction, txn.id)
    assert row is not None and (row.category_id, row.subcategory_id) == fuel


def test_apply_rule_rejects_builtin_and_missing_rules(client: TestClient, session: Session) -> None:
    grocery = category_ids(session, *GROCERY)
    builtin = Rule(
        match_type="contains",
        pattern="trader",
        category_id=grocery[0],
        subcategory_id=grocery[1],
        source="builtin",
    )
    session.add(builtin)
    session.commit()
    assert client.post("/api/rules/999999/apply").status_code == 404
    assert client.post(f"/api/rules/{builtin.id}/apply").status_code == 400


def test_a_regex_rule_with_a_broken_pattern_matches_nothing_instead_of_erroring(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    _pending(session, 1, seed, merchant_key="trader joes")
    session.commit()
    rule = _create_rule(client, "([unclosed", match_type="regex")
    assert rule["existing_matches"] == 0


# --- What a re-classify covers --------------------------------------------------------------


def test_classify_run_covers_uncategorized_and_needs_review_but_not_user_corrections(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = base(session)
    null_row = add_txn(session, seed, 1)
    fallback = add_txn(session, seed, 2, category=UNCATEGORIZED)
    flagged = add_txn(session, seed, 3, category=GROCERY, needs_review=True, classified_by="llm")
    settled = add_txn(session, seed, 4, category=GROCERY, classified_by="llm")
    corrected_fallback = add_txn(
        session, seed, 5, category=UNCATEGORIZED, needs_review=True, classified_by="user"
    )
    session.commit()

    seen: list[tuple[int, ...]] = []

    def fake_classify_transactions(session_factory, transaction_ids, *, cfg, group_id, progress_cb):
        seen.append(tuple(transaction_ids))
        return gateway.ClassifyResult(
            group_id=group_id, classified=len(seen[-1]), needs_review=0, llm_requests=0, cost_usd=0
        )

    monkeypatch.setattr(gateway, "classify_transactions", fake_classify_transactions)

    def run(**body: object) -> dict[str, object]:
        response = client.post("/api/classify/run", json=body)
        assert response.status_code == 202
        out: dict[str, object] = response.json()
        deadline = time.time() + 5
        while time.time() < deadline:
            if client.get(f"/api/jobs/{out['job_id']}").json()["status"] == "done":
                break
            time.sleep(0.02)
        return out

    first = run(only_unclassified=True)
    assert first["transaction_count"] == 3
    assert set(seen[-1]) == {null_row.id, fallback.id, flagged.id}

    everything = run(only_unclassified=False)
    assert set(seen[-1]) == {null_row.id, fallback.id, flagged.id, settled.id}
    assert everything["transaction_count"] == 4
    assert corrected_fallback.id not in seen[-1]  # a hand correction is never reconsidered


# --- Undo never overwrites later edits, and does not trust its token ------------------------


def _undo(client: TestClient, token: object) -> dict[str, object]:
    response = client.post("/api/review/undo", json=token)
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def test_undo_after_a_later_edit_leaves_that_edit_alone(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    first = _pending(session, 1, seed, merchant_key="trader joes")
    second = _pending(session, 2, seed, merchant_key="trader joes")
    third = _pending(session, 3, seed, merchant_key="trader joes")
    session.commit()
    uncategorized = category_ids(session, *UNCATEGORIZED)
    fuel = category_ids(session, "fuel", "gasoline")

    body = _accept(client, first.id, similar_ids=[second.id, third.id])
    # The user then re-categorizes the first row by hand, and deletes nothing else.
    assert (
        client.patch(
            f"/api/transactions/{first.id}",
            json={"category_key": "fuel", "subcategory_key": "gasoline"},
        ).status_code
        == 200
    )

    undone = _undo(client, body["undo"])
    assert undone["restored"] == 2
    assert undone["skipped"] == [first.id]

    session.expire_all()
    kept = session.get(Transaction, first.id)
    assert kept is not None
    assert (kept.category_id, kept.subcategory_id) == fuel  # the later edit survived
    assert kept.classified_by == "user" and kept.needs_review is False
    for txn_id in (second.id, third.id):
        txn = session.get(Transaction, txn_id)
        assert txn is not None
        assert (txn.category_id, txn.subcategory_id) == uncategorized
        assert txn.needs_review is True


def test_undo_twice_is_a_no_op_the_second_time(client: TestClient, session: Session) -> None:
    seed = base(session)
    first = _pending(session, 1, seed, merchant_key="trader joes")
    session.commit()
    body = _accept(client, first.id)
    assert _undo(client, body["undo"])["restored"] == 1
    # The row now equals its pre-accept state, not the accept's: nothing to revert.
    again = _undo(client, body["undo"])
    assert again["restored"] == 0 and again["skipped"] == [first.id]


def test_undo_leaves_a_rule_edited_after_the_accept(client: TestClient, session: Session) -> None:
    seed = base(session)
    first = _pending(session, 1, seed, merchant_key="trader joes")
    session.commit()
    fuel = category_ids(session, "fuel", "gasoline")

    body = _accept(client, first.id, create_rule=True)
    rule = session.execute(select(Rule).where(Rule.pattern == "trader joes")).scalar_one()
    edited = client.patch(
        f"/api/rules/{rule.id}", json={"category_key": "fuel", "subcategory_key": "gasoline"}
    )
    assert edited.status_code == 200

    undone = _undo(client, body["undo"])
    assert undone["restored"] == 1 and undone["rule_skipped"] is True
    session.expire_all()
    kept = session.execute(select(Rule).where(Rule.pattern == "trader joes")).scalar_one()
    assert (kept.category_id, kept.subcategory_id) == fuel  # not deleted, not reverted


def test_undo_leaves_a_rule_that_was_deleted_and_recreated(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    first = _pending(session, 1, seed, merchant_key="trader joes")
    session.commit()
    body = _accept(client, first.id, create_rule=True)
    rule = session.execute(select(Rule).where(Rule.pattern == "trader joes")).scalar_one()
    assert client.delete(f"/api/rules/{rule.id}").status_code == 204
    assert _undo(client, body["undo"])["rule_skipped"] is True


def test_undo_deletes_the_rule_it_created_when_nothing_changed_it(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    first = _pending(session, 1, seed, merchant_key="trader joes")
    session.commit()
    body = _accept(client, first.id, create_rule=True)
    undone = _undo(client, body["undo"])
    assert undone["rule_skipped"] is False and undone["skipped"] == []
    session.expire_all()
    assert session.execute(select(Rule).where(Rule.pattern == "trader joes")).scalars().all() == []


def _forged(txn_id: int, **after: object) -> dict[str, object]:
    state: dict[str, object] = {
        "category_id": None,
        "subcategory_id": None,
        "confidence": None,
        "classified_by": None,
        "needs_review": True,
        "kind": "purchase",
        "is_spend": True,
        "merchant_canonical": None,
    }
    return {
        "transactions": [
            {"transaction_id": txn_id, "before": {**state, **after}, "after": state},
        ],
        "rule": None,
    }


def test_undo_rejects_a_token_with_unknown_enums_or_taxonomy_ids(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    txn = _pending(session, 1, seed)
    session.commit()

    assert client.post("/api/review/undo", json=_forged(txn.id, kind="bogus")).status_code == 422
    assert (
        client.post("/api/review/undo", json=_forged(txn.id, classified_by="hacked")).status_code
        == 422
    )
    assert client.post("/api/review/undo", json=_forged(txn.id, confidence=7)).status_code == 422
    unknown = client.post(
        "/api/review/undo", json=_forged(txn.id, category_id=99999, subcategory_id=99999)
    )
    assert unknown.status_code == 400
    mismatched = client.post(
        "/api/review/undo",
        json=_forged(
            txn.id,
            category_id=category_ids(session, *GROCERY)[0],
            subcategory_id=category_ids(session, "fuel", "gasoline")[1],
        ),
    )
    assert mismatched.status_code == 400
    half = client.post("/api/review/undo", json=_forged(txn.id, category_id=1))
    assert half.status_code == 400


def test_undo_only_touches_the_users_exact_rule_for_the_accepted_merchant(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    first = _pending(session, 1, seed, merchant_key="trader joes")
    grocery = category_ids(session, *GROCERY)
    builtin = Rule(
        match_type="contains",
        pattern="shell",
        category_id=grocery[0],
        subcategory_id=grocery[1],
        source="builtin",
    )
    unrelated = Rule(
        match_type="exact",
        pattern="some other merchant",
        category_id=grocery[0],
        subcategory_id=grocery[1],
        user_id=seed[0].id,
        source="user",
    )
    session.add_all([builtin, unrelated])
    session.commit()

    body = _accept(client, first.id)  # a plain accept, no rule
    state = {
        "category_id": grocery[0],
        "subcategory_id": grocery[1],
        "kind_override": None,
        "enabled": True,
    }
    for rule in (builtin, unrelated):
        token = {**body["undo"], "rule": {"rule_id": rule.id, "before": None, "after": state}}  # type: ignore[dict-item]
        undone = _undo(client, token)
        assert undone["rule_skipped"] is True, rule.pattern
    session.expire_all()
    assert session.get(Rule, builtin.id) is not None
    assert session.get(Rule, unrelated.id) is not None


# --- Apply to existing: preview and undo -----------------------------------------------------


def test_apply_preview_reports_matches_users_and_kind_changes_without_changing_anything(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    a = _pending(session, 1, seed, merchant_key="trader joes 1")
    _pending(session, 2, seed, merchant_key="trader joes 2")
    session.commit()

    rule = _create_rule(client, "trader joes", kind="fee")
    preview = client.get(f"/api/rules/{rule['id']}/apply-preview")
    assert preview.status_code == 200
    assert preview.json() == {
        "matches": 2,
        "user_count": 1,
        "kind_changes": 2,  # both purchases would become fees
        "all_users": True,  # rules made on the Rules screen have no owner
    }
    session.expire_all()
    row = session.get(Transaction, a.id)
    assert row is not None and row.kind == "purchase" and row.needs_review is True

    assert client.get("/api/rules/999999/apply-preview").status_code == 404


def test_apply_returns_an_undo_token_that_reverses_it(client: TestClient, session: Session) -> None:
    seed = base(session)
    a = _pending(session, 1, seed, merchant_key="trader joes 1")
    b = _pending(session, 2, seed, merchant_key="trader joes 2")
    session.commit()
    uncategorized = category_ids(session, *UNCATEGORIZED)

    rule = _create_rule(client, "trader joes", kind="fee")
    applied = client.post(f"/api/rules/{rule['id']}/apply").json()
    assert applied["updated"] == 2
    session.expire_all()
    changed = session.get(Transaction, a.id)
    assert changed is not None and changed.kind == "fee" and changed.is_spend is True

    # One row is hand-edited afterwards: undo must not touch it.
    assert (
        client.patch(
            f"/api/transactions/{b.id}",
            json={"category_key": "fuel", "subcategory_key": "gasoline"},
        ).status_code
        == 200
    )
    undone = _undo(client, applied["undo"])
    assert undone["restored"] == 1 and undone["skipped"] == [b.id]

    session.expire_all()
    back = session.get(Transaction, a.id)
    assert back is not None
    assert (back.category_id, back.subcategory_id) == uncategorized
    assert back.kind == "purchase" and back.needs_review is True and back.classified_by == "llm"
    kept = session.get(Transaction, b.id)
    assert kept is not None and kept.classified_by == "user"
    # The rule itself is not part of the token: it survives, ready to be applied again.
    assert session.get(Rule, rule["id"]) is not None
