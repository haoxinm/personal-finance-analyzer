"""One-time edits vs merchant rules.

An edit of a transaction (PATCH, bulk update, Review accept) is a one-time patch: it changes that
one transaction, always wins over rules and re-runs, and writes no merchant memory. Only the
explicit "always categorize" action (`create_rule`) creates a rule, and a rule never overrides a
patch. Precedence: one-time edit > user rules > built-ins / cache / LLM.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from spend_analyzer.api.services import gateway
from spend_analyzer.classify import cascade
from spend_analyzer.config import LLMConfig
from spend_analyzer.db.models import MerchantMap, Rule, Transaction
from spend_analyzer.db.session import make_session_factory
from tests.api.seed import add_txn, base, category_ids

GROCERY = ("grocery", "grocery_stores")
FUEL = ("fuel", "gasoline")
UNCATEGORIZED = ("others", "uncategorized")


def _patch(
    client: TestClient, txn_id: int, category: tuple[str, str] = FUEL, **extra: object
) -> dict[str, object]:
    response = client.patch(
        f"/api/transactions/{txn_id}",
        json={"category_key": category[0], "subcategory_key": category[1], **extra},
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


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
    return add_txn(
        session,
        seed,  # type: ignore[arg-type]
        index,
        category=UNCATEGORIZED,
        needs_review=True,
        classified_by="llm",
        **kwargs,  # type: ignore[arg-type]
    )


def _rules_for(session: Session, pattern: str) -> list[Rule]:
    session.expire_all()
    return list(session.execute(select(Rule).where(Rule.pattern == pattern)).scalars())


def _category_of(session: Session, txn_id: int) -> tuple[int | None, int | None]:
    session.expire_all()
    txn = session.get(Transaction, txn_id)
    assert txn is not None
    return txn.category_id, txn.subcategory_id


def _classify(engine: Engine, ids: Sequence[int]) -> cascade.ClassifyResult:
    """The cascade over exactly `ids`, with no LLM configured (D2)."""
    return cascade.classify_transactions(
        make_session_factory(engine),
        list(ids),
        cfg=LLMConfig(),
        group_id="g",
        progress_cb=lambda done, total, cost: None,
    )


def _user_rule(
    client: TestClient, pattern: str, category: tuple[str, str] = GROCERY, **extra: object
) -> dict[str, object]:
    response = client.post(
        "/api/rules",
        json={
            "pattern": pattern,
            "match_type": "exact",
            "category_key": category[0],
            "subcategory_key": category[1],
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


# --- A patch is one-time ---------------------------------------------------------------------


def test_patch_changes_only_that_transaction_and_writes_no_merchant_memory(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    edited = _pending(session, 1, seed, merchant_key="zorblax mart")
    sibling = _pending(session, 2, seed, merchant_key="zorblax mart")
    session.commit()

    _patch(client, edited.id, GROCERY)

    session.expire_all()
    row = session.get(Transaction, edited.id)
    assert row is not None
    assert row.classified_by == "user" and row.needs_review is False and row.confidence == 1.0
    assert (row.category_id, row.subcategory_id) == category_ids(session, *GROCERY)
    # No pin, no rule: nothing about the merchant was remembered.
    assert session.execute(select(MerchantMap)).scalars().all() == []
    assert session.execute(select(Rule)).scalars().all() == []
    untouched = session.get(Transaction, sibling.id)
    assert untouched is not None
    assert untouched.classified_by == "llm" and untouched.needs_review is True
    assert (untouched.category_id, untouched.subcategory_id) == category_ids(
        session, *UNCATEGORIZED
    )


def test_a_patched_transaction_does_not_decide_the_next_one_of_that_merchant(
    client: TestClient, session: Session, engine: Engine
) -> None:
    seed = base(session)
    edited = _pending(session, 1, seed, merchant_key="zorblax mart")
    session.commit()
    _patch(client, edited.id, FUEL)

    # The next import of the same merchant arrives uncategorized; nothing pins it to fuel.
    later = add_txn(session, seed, 2, merchant_key="zorblax mart")
    session.commit()
    result = _classify(engine, [edited.id, later.id])

    assert result.classified == 1  # the patched row was not even considered
    assert _category_of(session, edited.id) == category_ids(session, *FUEL)
    assert _category_of(session, later.id) == category_ids(session, *UNCATEGORIZED)


def test_a_one_time_edit_does_not_stop_a_user_rule_from_deciding_other_transactions(
    client: TestClient, session: Session, engine: Engine
) -> None:
    seed = base(session)
    edited = _pending(session, 1, seed, merchant_key="zorblax mart")
    session.commit()
    _patch(client, edited.id, FUEL)
    _user_rule(client, "zorblax mart", GROCERY)

    later = add_txn(session, seed, 2, merchant_key="zorblax mart")
    session.commit()
    _classify(engine, [edited.id, later.id])

    assert _category_of(session, edited.id) == category_ids(session, *FUEL)  # the patch wins
    assert _category_of(session, later.id) == category_ids(session, *GROCERY)  # the rule decides
    assert session.get(Transaction, later.id).classified_by == "user_rule"  # type: ignore[union-attr]


def test_bulk_update_patches_each_row_once_and_remembers_nothing(
    client: TestClient, session: Session, engine: Engine
) -> None:
    seed = base(session)
    a = _pending(session, 1, seed, merchant_key="quuxco")
    b = _pending(session, 2, seed, merchant_key="quuxco")
    other = _pending(session, 3, seed, merchant_key="quuxco")
    session.commit()

    response = client.post(
        "/api/transactions/bulk-update",
        json={
            "transaction_ids": [a.id, b.id],
            "category_key": FUEL[0],
            "subcategory_key": FUEL[1],
        },
    )
    assert response.json() == {"updated": 2}

    session.expire_all()
    for txn_id in (a.id, b.id):
        row = session.get(Transaction, txn_id)
        assert row is not None and row.classified_by == "user" and not row.needs_review
    assert session.execute(select(MerchantMap)).scalars().all() == []
    assert session.execute(select(Rule)).scalars().all() == []

    _classify(engine, [a.id, b.id, other.id])
    assert _category_of(session, a.id) == category_ids(session, *FUEL)
    assert _category_of(session, b.id) == category_ids(session, *FUEL)
    assert _category_of(session, other.id) != category_ids(session, *FUEL)  # not affected


def test_kind_edits_on_categorized_rows_are_patches_but_notes_are_not(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    kind_edit = add_txn(session, seed, 1, category=GROCERY, classified_by="llm")
    note_edit = _pending(session, 2, seed)
    bulk_kind = add_txn(session, seed, 3, category=GROCERY, classified_by="llm")
    session.commit()

    assert (
        client.patch(f"/api/transactions/{kind_edit.id}", json={"kind": "fee"}).status_code == 200
    )
    assert (
        client.patch(f"/api/transactions/{note_edit.id}", json={"notes": "hi"}).status_code == 200
    )
    bulk = client.post(
        "/api/transactions/bulk-update", json={"transaction_ids": [bulk_kind.id], "kind": "fee"}
    )
    assert bulk.json() == {"updated": 1}

    session.expire_all()
    assert session.get(Transaction, kind_edit.id).classified_by == "user"  # type: ignore[union-attr]
    assert session.get(Transaction, bulk_kind.id).classified_by == "user"  # type: ignore[union-attr]
    assert session.get(Transaction, note_edit.id).classified_by == "llm"  # type: ignore[union-attr]


def test_kind_only_edit_on_an_uncategorized_row_does_not_freeze_its_category(
    client: TestClient, session: Session, engine: Engine
) -> None:
    """Regression: a kind-only edit used to mark an uncategorized row `user`, which every
    re-classify, rule and LLM pass skips, so it stayed uncategorized forever."""
    seed = base(session)
    parked = _pending(session, 1, seed, merchant_key="corner shop")
    bare = add_txn(session, seed, 2, merchant_key="no category", needs_review=True)
    session.commit()

    assert client.patch(f"/api/transactions/{parked.id}", json={"kind": "fee"}).status_code == 200
    bulk = client.post(
        "/api/transactions/bulk-update", json={"transaction_ids": [bare.id], "kind": "fee"}
    )
    assert bulk.json() == {"updated": 1}

    session.expire_all()
    for txn_id in (parked.id, bare.id):
        row = session.get(Transaction, txn_id)
        assert row is not None
        assert row.kind == "fee" and row.is_spend is True  # the edit itself is applied
        assert row.classified_by != "user"  # ... but the row is still open to classification

    # A rule can therefore still categorize it later.
    rule = Rule(
        match_type="exact",
        pattern="corner shop",
        category_id=category_ids(session, *FUEL)[0],
        subcategory_id=category_ids(session, *FUEL)[1],
        priority=50,
        enabled=True,
        source="user",
    )
    session.add(rule)
    session.commit()
    _classify(engine, [parked.id])
    assert _category_of(session, parked.id) == category_ids(session, *FUEL)


# --- "Always categorize" is the explicit merchant rule ---------------------------------------


def test_patch_with_create_rule_saves_one_exact_rule_and_patches_the_transaction(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    edited = _pending(session, 1, seed, merchant_key="zorblax mart")
    sibling = _pending(session, 2, seed, merchant_key="zorblax mart")
    session.commit()

    _patch(client, edited.id, GROCERY, create_rule=True)
    _patch(client, edited.id, FUEL, create_rule=True)  # choosing again updates it in place

    rules = _rules_for(session, "zorblax mart")
    assert len(rules) == 1 and rules[0].match_type == "exact" and rules[0].source == "user"
    assert (rules[0].category_id, rules[0].subcategory_id) == category_ids(session, *FUEL)
    row = session.get(Transaction, edited.id)
    assert row is not None and row.classified_by == "user"  # the current one is patched too
    assert session.execute(select(MerchantMap)).scalars().all() == []
    # An existing sibling changes only through an explicit "Apply to existing".
    assert _category_of(session, sibling.id) == category_ids(session, *UNCATEGORIZED)

    applied = client.post(f"/api/rules/{rules[0].id}/apply")
    assert applied.json()["updated"] == 1
    assert _category_of(session, sibling.id) == category_ids(session, *FUEL)


def test_a_merchant_rule_never_overrides_an_earlier_patch(
    client: TestClient, session: Session, engine: Engine
) -> None:
    seed = base(session)
    patched = _pending(session, 1, seed, merchant_key="zorblax mart")
    other = _pending(session, 2, seed, merchant_key="zorblax mart")
    session.commit()
    _patch(client, patched.id, FUEL)

    # "Always categorize Trader Joe's as grocery" on the sibling...
    _accept(client, other.id, create_rule=True, category=GROCERY)
    rule = _rules_for(session, "zorblax mart")[0]

    # ...does not touch the patched row via apply-to-existing, a classify run, or the API run.
    assert client.post(f"/api/rules/{rule.id}/apply").json()["updated"] == 0
    _classify(engine, [patched.id, other.id])
    assert _category_of(session, patched.id) == category_ids(session, *FUEL)
    assert session.get(Transaction, patched.id).classified_by == "user"  # type: ignore[union-attr]


def test_a_new_rule_never_overrides_a_patch_even_with_higher_priority(
    client: TestClient, session: Session, engine: Engine
) -> None:
    seed = base(session)
    patched = _pending(session, 1, seed, merchant_key="zorblax mart")
    session.commit()
    _patch(client, patched.id, FUEL)

    rule = _user_rule(client, "zorblax mart", GROCERY, priority=1)
    assert rule["existing_matches"] == 0  # the preview already excludes the patched row
    assert client.post(f"/api/rules/{rule['id']}/apply").json()["updated"] == 0
    _classify(engine, [patched.id])
    assert _category_of(session, patched.id) == category_ids(session, *FUEL)


# --- Review accept ---------------------------------------------------------------------------


def test_accept_is_a_patch_and_its_similar_rows_are_patches_too(
    client: TestClient, session: Session, engine: Engine
) -> None:
    seed = base(session)
    first = _pending(session, 1, seed, merchant_key="zorblax mart")
    similar = _pending(session, 2, seed, merchant_key="zorblax mart")
    session.commit()

    body = _accept(client, first.id, similar_ids=[similar.id])
    assert body["similar_ids"] == [similar.id]

    session.expire_all()
    for txn_id in (first.id, similar.id):
        row = session.get(Transaction, txn_id)
        assert row is not None and row.classified_by == "user" and not row.needs_review
    assert session.execute(select(MerchantMap)).scalars().all() == []
    assert session.execute(select(Rule)).scalars().all() == []

    # A future transaction of the merchant is not decided by the accept.
    later = add_txn(session, seed, 3, merchant_key="zorblax mart")
    session.commit()
    _classify(engine, [later.id])
    assert _category_of(session, later.id) == category_ids(session, *UNCATEGORIZED)


def test_always_categorize_accept_decides_future_transactions_through_its_rule(
    client: TestClient, session: Session, engine: Engine
) -> None:
    seed = base(session)
    first = _pending(session, 1, seed, merchant_key="zorblax mart")
    session.commit()
    _accept(client, first.id, create_rule=True)

    later = add_txn(session, seed, 2, merchant_key="zorblax mart")
    session.commit()
    _classify(engine, [later.id])
    assert _category_of(session, later.id) == category_ids(session, *GROCERY)
    assert session.get(Transaction, later.id).classified_by == "user_rule"  # type: ignore[union-attr]


def test_undoing_a_plain_accept_restores_the_rows_and_leaves_rules_alone(
    client: TestClient, session: Session
) -> None:
    seed = base(session)
    first = _pending(session, 1, seed, merchant_key="zorblax mart")
    similar = _pending(session, 2, seed, merchant_key="zorblax mart")
    session.commit()
    existing = _user_rule(client, "zorblax mart", FUEL)

    body = _accept(client, first.id, similar_ids=[similar.id])
    assert body["undo"]["rule"] is None  # type: ignore[index]
    undone = client.post("/api/review/undo", json=body["undo"])
    assert undone.json()["restored"] == 2

    session.expire_all()
    for txn_id in (first.id, similar.id):
        row = session.get(Transaction, txn_id)
        assert row is not None
        assert row.classified_by == "llm" and row.needs_review is True
        assert (row.category_id, row.subcategory_id) == category_ids(session, *UNCATEGORIZED)
    rules = _rules_for(session, "zorblax mart")
    assert [r.id for r in rules] == [existing["id"]]  # untouched
    assert (rules[0].category_id, rules[0].subcategory_id) == category_ids(session, *FUEL)


def test_undo_token_has_no_merchant_map_part(client: TestClient, session: Session) -> None:
    seed = base(session)
    first = _pending(session, 1, seed, merchant_key="zorblax mart")
    session.commit()
    body = _accept(client, first.id, create_rule=True)
    assert set(body["undo"]) == {"transactions", "rule"}  # type: ignore[arg-type]
    assert body["undo"]["rule"]["before"] is None  # type: ignore[index]


# --- Automated paths through the API ---------------------------------------------------------


def test_the_classify_endpoint_and_its_preview_never_include_patched_rows(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = base(session)
    patched = _pending(session, 1, seed, merchant_key="zorblax mart")
    pending = _pending(session, 2, seed, merchant_key="quuxco")
    session.commit()
    _patch(client, patched.id, FUEL)

    preview = client.get("/api/classify/preview").json()
    assert [row["merchant_key"] for row in preview] == ["quuxco"]

    seen: list[tuple[int, ...]] = []

    def fake(session_factory, transaction_ids, *, cfg, group_id, progress_cb):  # type: ignore[no-untyped-def]
        seen.append(tuple(transaction_ids))
        return gateway.ClassifyResult(
            group_id=group_id, classified=0, needs_review=0, llm_requests=0, cost_usd=0
        )

    monkeypatch.setattr(gateway, "classify_transactions", fake)
    response = client.post("/api/classify/run", json={"only_unclassified": False})
    deadline = time.time() + 5
    while time.time() < deadline:
        if client.get(f"/api/jobs/{response.json()['job_id']}").json()["status"] == "done":
            break
        time.sleep(0.02)
    assert seen[-1] == (pending.id,)
