"""One-click suggestions over the API: candidates endpoint, list payload, conflict filter.

Applying a suggestion is an ordinary one-time edit (`PATCH /transactions/{id}`), so these tests
also run that path end to end. Nothing here may reach the LLM.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from personal_finance_analyzer.classify.rules import sync_builtin_rules
from personal_finance_analyzer.db.models import MerchantMap, Rule, Transaction
from tests.api.seed import add_txn, base, category_ids
from tests.llm_fakes import FakeLLM
from tests.llm_fakes import install as install_fake_llm

DINING = ("restaurant", "dine_in")
WHOLESALE = ("grocery", "wholesale_clubs")
GAS = ("fuel", "gasoline")
UNCATEGORIZED = ("others", "uncategorized")


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """Every test here runs against a fake LLM with nothing queued, and checks it stayed unused."""
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


def _llm_map(session: Session, merchant_key: str, category: tuple[str, str]) -> None:
    category_id, subcategory_id = category_ids(session, *category)
    session.add(
        MerchantMap(
            merchant_key=merchant_key,
            category_id=category_id,
            subcategory_id=subcategory_id,
            source="llm",
            confidence=0.5,
        )
    )


def _seed_conflicts(session: Session) -> dict[str, Transaction]:
    """`conflict`: the built-in rule says wholesale, the saved LLM answer says dining (a winning
    user rule would settle the row, so it is not a conflict). `edited`: patched by hand.
    `plain`: no layer knows it. `agree`: one layer only."""
    seed = base(session)
    sync_builtin_rules(session)
    _llm_map(session, "costco whse", DINING)
    rows = {
        "conflict": add_txn(
            session,
            seed,
            1,
            merchant_key="costco whse",
            category=WHOLESALE,
            classified_by="builtin_rule",
            posted_date="2026-01-04",
        ),
        "edited": add_txn(
            session,
            seed,
            2,
            merchant_key="costco online",
            category=GAS,
            classified_by="user",
            posted_date="2026-01-03",
        ),
        "plain": add_txn(
            session,
            seed,
            3,
            merchant_key="nobody",
            category=UNCATEGORIZED,
            posted_date="2026-01-02",
        ),
        "agree": add_txn(
            session,
            seed,
            4,
            merchant_key="starbucks 1",
            category=DINING,
            classified_by="builtin_rule",
            posted_date="2026-01-01",
        ),
    }
    session.commit()
    return rows


def test_candidates_endpoint_lists_every_layer_in_precedence_order(
    client: TestClient, session: Session, fake_llm: FakeLLM
) -> None:
    rows = _seed_conflicts(session)

    response = client.get(f"/api/transactions/{rows['conflict'].id}/candidates")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transaction_id"] == rows["conflict"].id
    assert body["conflict"] is True
    assert [
        (c["source"], c["category_key"], c["subcategory_key"], c["label"], c["is_current"])
        for c in body["candidates"]
    ] == [
        ("builtin_rule", "grocery", "wholesale_clubs", "COSTCO", True),
        ("llm", "restaurant", "dine_in", "learned from LLM", False),
    ]
    assert body["candidates"][0]["rule_id"] is not None
    assert fake_llm.requests == []


def test_a_row_settled_by_a_user_rule_is_not_a_conflict_though_lower_layers_disagree(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    _user_rule(session, seed[0].id, "costco", DINING)
    _llm_map(session, "costco whse", WHOLESALE)
    txn = add_txn(
        session, seed, 1, merchant_key="costco whse", category=DINING, classified_by="user_rule"
    )
    session.commit()

    body = client.get(f"/api/transactions/{txn.id}/candidates").json()

    assert body["conflict"] is False
    assert [c["source"] for c in body["candidates"]] == ["user_rule", "builtin_rule", "llm"]
    assert client.get("/api/transactions", params={"conflict": True}).json()["total"] == 0


def test_candidates_endpoint_for_an_edited_row_marks_the_edit_current(
    client: TestClient, session: Session
) -> None:
    rows = _seed_conflicts(session)

    body = client.get(f"/api/transactions/{rows['edited'].id}/candidates").json()

    assert body["conflict"] is False
    assert [(c["source"], c["is_current"]) for c in body["candidates"]] == [
        ("one_time_edit", True),
        ("builtin_rule", False),
    ]


def test_candidates_endpoint_404s_for_an_unknown_transaction(client: TestClient) -> None:
    assert client.get("/api/transactions/999/candidates").status_code == 404


def test_list_payload_only_carries_candidates_when_asked(
    client: TestClient, session: Session
) -> None:
    _seed_conflicts(session)

    plain = client.get("/api/transactions").json()["items"]
    assert all(item.get("candidates") is None and item.get("conflict") is None for item in plain)

    flagged = client.get("/api/transactions", params={"include_conflict": True}).json()["items"]
    assert {item["merchant_key"]: item["conflict"] for item in flagged} == {
        "costco whse": True,
        "costco online": False,
        "nobody": False,
        "starbucks 1": False,
    }
    assert all(item["candidates"] is None for item in flagged)

    full = client.get("/api/transactions", params={"include_candidates": True}).json()["items"]
    by_key = {item["merchant_key"]: item for item in full}
    assert len(by_key["costco whse"]["candidates"]) == 2
    assert by_key["nobody"]["candidates"] == []
    assert by_key["costco whse"]["conflict"] is True


def test_review_queue_items_carry_candidates_and_conflict(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    _llm_map(session, "costco whse", DINING)
    add_txn(
        session,
        seed,
        1,
        merchant_key="costco whse",
        category=WHOLESALE,
        needs_review=True,
        classified_by="llm",
    )
    add_txn(session, seed, 2, category=WHOLESALE, merchant_key="quiet", classified_by="llm")
    session.commit()

    response = client.get(
        "/api/transactions",
        params={"needs_review": True, "include_non_spend": True, "include_candidates": True},
    )

    (item,) = response.json()["items"]
    assert item["conflict"] is True
    assert [c["source"] for c in item["candidates"]] == ["builtin_rule", "llm"]
    llm = item["candidates"][1]
    assert llm["is_current"] is False  # the built-in rule outranks the saved LLM answer
    assert llm["label"] == "learned from LLM"


def test_conflict_filter_selects_only_conflicting_rows_and_ids_agree(
    client: TestClient, session: Session
) -> None:
    rows = _seed_conflicts(session)

    yes = client.get("/api/transactions", params={"conflict": True}).json()
    assert [i["id"] for i in yes["items"]] == [rows["conflict"].id]
    assert yes["total"] == 1

    no = client.get("/api/transactions", params={"conflict": False}).json()
    assert sorted(i["id"] for i in no["items"]) == sorted(
        rows[k].id for k in ("edited", "plain", "agree")
    )
    assert no["total"] == 3

    ids = client.get("/api/transactions/ids", params={"conflict": True}).json()
    assert ids == {"ids": [rows["conflict"].id], "total": 1}
    export = client.get("/api/export", params={"conflict": True})
    assert export.status_code == 200
    assert str(rows["conflict"].id) in export.text and "starbucks 1" not in export.text


def test_conflict_filter_composes_with_other_filters_and_paginates(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    sync_builtin_rules(session)
    _llm_map(session, "costco whse", DINING)
    for i in range(7):
        add_txn(
            session,
            seed,
            i,
            merchant_key="costco whse",
            category=WHOLESALE,
            amount_minor=100 * (i + 1),
            posted_date=f"2026-01-{i + 1:02d}",
        )
    for i in range(7, 10):
        add_txn(session, seed, i, merchant_key="quiet", category=UNCATEGORIZED)
    session.commit()

    first = client.get(
        "/api/transactions",
        params={"conflict": True, "page_size": 3, "page": 1, "sort": "amount_minor"},
    ).json()
    last = client.get(
        "/api/transactions",
        params={"conflict": True, "page_size": 3, "page": 3, "sort": "amount_minor"},
    ).json()
    narrowed = client.get(
        "/api/transactions", params={"conflict": True, "amount_min_minor": 500}
    ).json()

    assert first["total"] == last["total"] == 7
    assert [i["amount_minor"] for i in first["items"]] == [100, 200, 300]
    assert [i["amount_minor"] for i in last["items"]] == [700]
    assert narrowed["total"] == 3


def test_applying_a_suggestion_is_a_one_time_edit_that_survives_and_stays_listed(
    client: TestClient, session: Session, fake_llm: FakeLLM
) -> None:
    rows = _seed_conflicts(session)
    txn_id = rows["conflict"].id
    lower = client.get(f"/api/transactions/{txn_id}/candidates").json()["candidates"][1]

    patched = client.patch(
        f"/api/transactions/{txn_id}",
        json={
            "category_key": lower["category_key"],
            "subcategory_key": lower["subcategory_key"],
            "create_rule": False,
        },
    )

    assert patched.status_code == 200, patched.text
    assert patched.json()["subcategory_key"] == "dine_in"
    after = client.get(f"/api/transactions/{txn_id}/candidates").json()
    assert after["conflict"] is False  # the edit settles it
    assert [(c["source"], c["is_current"]) for c in after["candidates"]] == [
        ("one_time_edit", True),
        ("builtin_rule", False),
        ("llm", False),
    ]
    session.expire_all()
    stored = session.get(Transaction, txn_id)
    assert stored is not None and stored.classified_by == "user"
    assert client.get("/api/transactions", params={"conflict": True}).json()["total"] == 0
    assert fake_llm.requests == []


def test_applying_a_suggestion_through_accept_carries_the_kind_and_undoes_cleanly(
    client: TestClient, session: Session, fake_llm: FakeLLM
) -> None:
    """The UI applies a chip with `POST /review/accept` (the same one-time edit as the PATCH, but
    it returns an undo token), passing the rule's kind override along."""
    rows = _seed_conflicts(session)
    txn_id = rows["conflict"].id

    accepted = client.post(
        "/api/review/accept",
        json={
            "transaction_id": txn_id,
            "category_key": "grocery",
            "subcategory_key": "wholesale_clubs",
            "create_rule": False,
            "similar_ids": [],
            "kind": "refund",
        },
    )

    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["transaction"]["kind"] == "refund"
    assert body["transaction"]["subcategory_key"] == "wholesale_clubs"
    session.expire_all()
    stored = session.get(Transaction, txn_id)
    assert stored is not None and stored.classified_by == "user" and not stored.is_spend

    undone = client.post("/api/review/undo", json=body["undo"])

    assert undone.status_code == 200, undone.text
    session.expire_all()
    restored = session.get(Transaction, txn_id)
    assert restored is not None
    assert (restored.kind, restored.classified_by, restored.is_spend) == (
        "purchase",
        "builtin_rule",
        True,
    )
    assert fake_llm.requests == []
