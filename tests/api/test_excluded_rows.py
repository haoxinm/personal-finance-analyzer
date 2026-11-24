"""Excluded rows: out of every total by default, kept through a re-import, never re-added by an
overlapping statement."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.models import User
from tests.api.fix_support import (
    BASE_ROWS,
    import_card,
    reparse_via_api,
    rows_of,
    statement_of,
)


def _exclude(client: TestClient, *ids: int, excluded: bool = True) -> dict[str, object]:
    response = client.post(
        "/api/transactions/bulk-exclude", json={"transaction_ids": list(ids), "excluded": excluded}
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def test_an_excluded_row_is_hidden_everywhere_by_default(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    coffee = rows_of(session, statement_id)["COFFEE SHOP"]
    response = client.patch(f"/api/transactions/{coffee.id}", json={"excluded": True})
    assert response.status_code == 200 and response.json()["excluded"] is True

    listed = client.get("/api/transactions").json()
    assert listed["total"] == 2 and coffee.id not in [t["id"] for t in listed["items"]]
    assert coffee.id not in client.get("/api/transactions/ids").json()["ids"]
    assert "COFFEE SHOP" not in client.get("/api/export").text
    assert sum(r["total_minor"] for r in client.get("/api/analytics/summary").json()) == 8743 - 1234
    merchants = client.get("/api/analytics/top-merchants").json()
    assert sum(m["total_minor"] for m in merchants) == 8743 - 1234

    # Review and classify skip it too.
    coffee_row = rows_of(session, statement_id)["COFFEE SHOP"]
    coffee_row.needs_review = True
    session.commit()
    assert client.get("/api/transactions", params={"needs_review": True}).json()["total"] == 0
    run = client.post("/api/classify/run", json={})
    assert run.json()["transaction_count"] == 2


def test_the_excluded_filter_and_source_filter(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    coffee = rows_of(session, statement_id)["COFFEE SHOP"]
    _exclude(client, coffee.id)

    only = client.get("/api/transactions", params={"excluded": "only"}).json()
    assert [t["id"] for t in only["items"]] == [coffee.id]
    both = client.get("/api/transactions", params={"excluded": "include"}).json()
    assert both["total"] == 3
    assert (
        coffee.id
        in client.get("/api/transactions/ids", params={"excluded": "include"}).json()["ids"]
    )
    assert "COFFEE SHOP" in client.get("/api/export", params={"excluded": "include"}).text
    assert "COFFEE SHOP" in client.get("/api/export", params={"excluded": "only"}).text
    assert client.get("/api/transactions", params={"excluded": "bogus"}).status_code == 422
    assert (
        client.get("/api/transactions", params={"source": "manual", "excluded": "include"}).json()[
            "total"
        ]
        == 0
    )


def test_bulk_exclude_and_include_report_exactly_what_changed(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    rows = rows_of(session, statement_id)
    grocery, coffee = rows["GROCERY MART"].id, rows["COFFEE SHOP"].id
    _exclude(client, coffee)

    body = _exclude(client, grocery, coffee, 9999)  # coffee already excluded, 9999 missing
    assert body == {"updated": 1, "changed_ids": [grocery]}
    undo = _exclude(client, *body["changed_ids"], excluded=False)  # type: ignore[arg-type]
    assert undo == {"updated": 1, "changed_ids": [grocery]}
    assert statement_of(session, statement_id).id == statement_id
    assert client.get(f"/api/statements/{statement_id}").json()["excluded_count"] == 1


def test_an_exclusion_survives_reparse_and_discard_edits_drops_it(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    coffee = rows_of(session, statement_id)["COFFEE SHOP"]
    _exclude(client, coffee.id)

    job = reparse_via_api(client, statement_id)
    assert (job["restored_edits"], job["unmatched_edits"]) == (1, 0)
    assert rows_of(session, statement_id)["COFFEE SHOP"].excluded is True

    job = reparse_via_api(client, statement_id, discard_edits=True)
    assert job["discarded_edits"] == 1
    assert rows_of(session, statement_id)["COFFEE SHOP"].excluded is False


def test_an_overlapping_statement_does_not_bring_the_excluded_row_back(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, user, issuer, _ = import_card(session, tmp_path, "a")
    coffee = rows_of(session, statement_id)["COFFEE SHOP"]
    _exclude(client, coffee.id)

    second_id, *_rest, result = import_card(
        session,
        tmp_path,
        "b",
        rows=[*BASE_ROWS, ("01/20/2026", "GAS STATION 77", "40.00")],
        closing="127.43",
        user=user,
        issuer=issuer,
    )
    assert (result.inserted, result.skipped_duplicates) == (1, 3)
    assert "COFFEE SHOP" not in rows_of(session, second_id)
    assert client.get("/api/transactions", params={"excluded": "only"}).json()["total"] == 1


def test_excluding_a_bogus_row_clears_the_reconciliation_delta(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(
        session, tmp_path, "a", rows=[*BASE_ROWS, ("01/31/2026", "UNRELATED LINE", "10.00")]
    )
    body = client.get(f"/api/statements/{statement_id}").json()
    assert body["reconciliation_delta_minor"] == 1000
    assert "reconciliation_mismatch" in body["attention"]

    bogus = rows_of(session, statement_id)["UNRELATED LINE"]
    assert client.patch(f"/api/transactions/{bogus.id}", json={"excluded": True}).status_code == 200
    body = client.get(f"/api/statements/{statement_id}").json()
    assert body["reconciliation_delta_minor"] == 0 and body["excluded_count"] == 1
    assert "reconciliation_mismatch" not in body["attention"]
    assert [s["reconciliation_delta_minor"] for s in client.get("/api/statements").json()] == [0]

    client.patch(f"/api/transactions/{bogus.id}", json={"excluded": False})
    assert (
        client.get(f"/api/statements/{statement_id}").json()["reconciliation_delta_minor"] == 1000
    )


def test_an_exclusion_is_handed_to_the_survivor_when_a_move_merges_duplicates(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, _alice, issuer, _ = import_card(session, tmp_path, "a")
    bob = User(name="bob")
    session.add(bob)
    session.commit()
    bob_statement, *_ = import_card(
        session,
        tmp_path,
        "b",
        closing="87.43",
        period="Statement Period: 01/01/2026 to 01/30/2026",
        user=bob,
        issuer=issuer,
    )
    coffee = rows_of(session, statement_id)["COFFEE SHOP"]
    _exclude(client, coffee.id)

    response = client.patch(f"/api/statements/{statement_id}", json={"user_id": bob.id})
    assert response.status_code == 200, response.text
    assert response.json()["duplicates_deleted"] == 3
    survivor = rows_of(session, bob_statement)["COFFEE SHOP"]
    assert survivor.excluded is True
