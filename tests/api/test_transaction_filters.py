"""Shared transaction filters: list, ids-only ("select all N matching") and export agree."""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from spend_analyzer.db.models import (
    Account,
    Category,
    Issuer,
    Statement,
    Subcategory,
    Transaction,
    User,
    utcnow_iso,
)


def _base(session: Session) -> tuple[User, list[Account], Statement]:
    user = User(name="Alex", is_default=True)
    issuer = Issuer(name="Chase", slug="chase", match_terms="[]")
    session.add_all([user, issuer])
    session.flush()
    accounts = [
        Account(
            user_id=user.id,
            issuer_id=issuer.id,
            account_type="credit",
            display_name=name,
            mask=name[-1],
        )
        for name in ("Card A", "Card B")
    ]
    session.add_all(accounts)
    session.flush()
    statement = Statement(
        account_id=accounts[0].id,
        file_sha256="e" * 64,
        original_name="s.pdf",
        status="parsed",
        ingested_at=utcnow_iso(),
    )
    session.add(statement)
    session.flush()
    return user, accounts, statement


def _category_ids(session: Session, category: str, subcategory: str) -> tuple[int, int]:
    cat = session.execute(select(Category).where(Category.key == category)).scalar_one()
    sub = session.execute(
        select(Subcategory).where(Subcategory.category_id == cat.id, Subcategory.key == subcategory)
    ).scalar_one()
    return cat.id, sub.id


def _add(
    session: Session,
    base: tuple[User, list[Account], Statement],
    index: int,
    *,
    account: int = 0,
    description: str | None = None,
    amount_minor: int = 1000,
    posted_date: str = "2026-01-05",
    category: tuple[str, str] | None = None,
    needs_review: bool = False,
    notes: str | None = None,
) -> Transaction:
    user, accounts, statement = base
    category_id: int | None = None
    subcategory_id: int | None = None
    if category is not None:
        category_id, subcategory_id = _category_ids(session, *category)
    txn = Transaction(
        statement_id=statement.id,
        account_id=accounts[account].id,
        user_id=user.id,
        posted_date=posted_date,
        description_raw=description or f"MERCHANT {index}",
        description_clean=description or f"Merchant {index}",
        merchant_key=f"merchant {index}",
        amount_minor=amount_minor,
        currency="USD",
        kind="purchase",
        is_spend=True,
        category_id=category_id,
        subcategory_id=subcategory_id,
        needs_review=needs_review,
        notes=notes,
        dedupe_hash=f"hash-{index}",
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    session.add(txn)
    return txn


def _ids(response_json: dict[str, object]) -> list[int]:
    items = response_json["items"]
    assert isinstance(items, list)
    return [item["id"] for item in items]


def test_ids_endpoint_returns_every_match_beyond_the_page_cap(
    client: TestClient, session: Session
) -> None:
    base = _base(session)
    for i in range(620):
        _add(session, base, i)
    session.commit()

    listed = client.get("/api/transactions", params={"page_size": 5000}).json()
    assert listed["total"] == 620
    assert len(listed["items"]) == 500  # the list endpoint is capped...

    response = client.get("/api/transactions/ids")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 620  # ...the ids endpoint is not
    assert len(set(body["ids"])) == 620


def test_ids_endpoint_honours_the_same_filters_as_the_list(
    client: TestClient, session: Session
) -> None:
    base = _base(session)
    for i in range(6):
        _add(session, base, i, account=i % 2, amount_minor=100 * (i + 1))
    session.commit()
    params = {"account_ids": [base[1][1].id], "amount_min_minor": 300}

    listed = client.get("/api/transactions", params=params).json()
    ids = client.get("/api/transactions/ids", params=params).json()
    assert sorted(ids["ids"]) == sorted(_ids(listed))
    assert ids["total"] == listed["total"] == 2


def test_uncategorized_filter_covers_null_and_fallback_but_not_real_categories(
    client: TestClient, session: Session
) -> None:
    base = _base(session)
    null_txn = _add(session, base, 1)
    fallback_txn = _add(session, base, 2, category=("others", "uncategorized"))
    _add(session, base, 3, category=("grocery", "grocery_stores"))
    session.commit()

    body = client.get("/api/transactions", params={"uncategorized": True}).json()
    assert sorted(_ids(body)) == sorted([null_txn.id, fallback_txn.id])
    body = client.get("/api/transactions", params={"uncategorized": False}).json()
    assert body["total"] == 1


def test_category_filter_still_works_without_dropping_other_filters(
    client: TestClient, session: Session
) -> None:
    base = _base(session)
    _add(session, base, 1, category=("grocery", "grocery_stores"), needs_review=True)
    _add(session, base, 2, category=("grocery", "grocery_stores"))
    _add(session, base, 3)
    session.commit()

    body = client.get(
        "/api/transactions", params={"category_keys": ["grocery"], "needs_review": True}
    ).json()
    assert body["total"] == 1
    body = client.get("/api/transactions", params={"category_keys": ["grocery"]}).json()
    assert body["total"] == 2


def test_sort_by_amount_both_directions_with_stable_paging(
    client: TestClient, session: Session
) -> None:
    base = _base(session)
    for i, amount in enumerate([500, 100, 300, 300, 200]):
        _add(session, base, i, amount_minor=amount)
    session.commit()

    asc = client.get("/api/transactions", params={"sort": "amount_minor"}).json()
    assert [t["amount_minor"] for t in asc["items"]] == [100, 200, 300, 300, 500]
    desc = client.get("/api/transactions", params={"sort": "-amount_minor"}).json()
    assert [t["amount_minor"] for t in desc["items"]] == [500, 300, 300, 200, 100]

    seen: list[int] = []
    for page in (1, 2, 3):
        body = client.get(
            "/api/transactions", params={"sort": "amount_minor", "page": page, "page_size": 2}
        ).json()
        seen.extend(_ids(body))
    assert len(seen) == len(set(seen)) == 5  # tied amounts never repeat or skip across pages


def test_bulk_update_counts_only_rows_actually_updated(
    client: TestClient, session: Session
) -> None:
    base = _base(session)
    first = _add(session, base, 1)
    second = _add(session, base, 2)
    session.commit()

    response = client.post(
        "/api/transactions/bulk-update",
        json={
            "transaction_ids": [first.id, second.id, 999_999],
            "category_key": "grocery",
            "subcategory_key": "grocery_stores",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"updated": 2}  # the missing id is not counted

    nothing = client.post(
        "/api/transactions/bulk-update", json={"transaction_ids": [first.id, second.id]}
    )
    assert nothing.json() == {"updated": 0}

    half = client.post(
        "/api/transactions/bulk-update",
        json={"transaction_ids": [first.id], "category_key": "grocery"},
    )
    assert half.status_code == 400
    unknown = client.post(
        "/api/transactions/bulk-update",
        json={"transaction_ids": [first.id], "category_key": "nope", "subcategory_key": "x"},
    )
    assert unknown.status_code == 400


def _csv_rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def test_export_applies_list_filters_and_defaults_to_everything(
    client: TestClient, session: Session
) -> None:
    base = _base(session)
    _add(session, base, 1, account=0)
    _add(session, base, 2, account=1)
    payment = _add(session, base, 3, account=0)
    payment.kind = "payment"
    session.commit()

    everything = _csv_rows(client.get("/api/export").text)
    assert len(everything) == 3  # unfiltered: every kind, as before

    filtered = _csv_rows(
        client.get(
            "/api/export", params={"account_ids": [base[1][0].id], "kinds": ["purchase"]}
        ).text
    )
    assert [r["description_clean"] for r in filtered] == ["Merchant 1"]

    one_user = _csv_rows(client.get("/api/export", params={"user_id": base[0].id}).text)
    assert len(one_user) == 3
    none = _csv_rows(client.get("/api/export", params={"user_ids": [base[0].id + 99]}).text)
    assert none == []


def test_export_neutralizes_csv_formula_cells_but_keeps_numbers_numeric(
    client: TestClient, session: Session
) -> None:
    base = _base(session)
    descriptions: Sequence[str] = (
        '=HYPERLINK("http://x","y")',
        "+CMD|calc",
        "-2+3",
        "@SUM(A1)",
        "\tTAB LEAD",
        " =1+1",
        "  @SUM(A1)",
        "\u00a0-5",
        "|calc!A1",
        " |calc",
        "Plain Merchant",
        "Shop - Main St",
    )
    for i, description in enumerate(descriptions):
        _add(session, base, i, description=description, amount_minor=-1500, notes=description)
    session.commit()

    rows = _csv_rows(client.get("/api/export").text)
    by_clean = {r["description_clean"]: r for r in rows}
    plain = ("Plain Merchant", "Shop - Main St")
    for description in descriptions:
        if description in plain:
            assert by_clean[description]["notes"] == description
        else:
            assert "'" + description in by_clean, description
            assert by_clean["'" + description]["notes"] == "'" + description
    assert all(r["amount_minor"] == "-1500" for r in rows)  # not prefixed: still a number

    # JSON is not a spreadsheet format and stays verbatim.
    assert "=HYPERLINK" in client.get("/api/export", params={"format": "json"}).text
