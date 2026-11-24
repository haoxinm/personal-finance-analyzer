"""Hand-added ("manual") rows: create, delete, survive a re-import, and get absorbed by a later
import that finds them on the bank's statement."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.models import Account, Rule, Statement, Transaction, User
from tests.api.fix_support import (
    BASE_ROWS,
    add_manual,
    import_card,
    reparse_via_api,
    rows_of,
    statement_of,
)


def _manual_rows(session: Session) -> list[Transaction]:
    session.expire_all()
    return list(
        session.scalars(
            select(Transaction).where(Transaction.source == "manual").order_by(Transaction.id)
        )
    )


def test_create_without_a_category_files_it_deterministically_or_flags_it(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, user, _issuer, _ = import_card(session, tmp_path, "a")
    body = add_manual(client, statement_id, description="Zzqx hardware", notes="for the shed")
    assert body["source"] == "manual" and body["excluded"] is False
    assert body["kind"] == "purchase"
    assert body["notes"] == "for the shed"
    assert body["needs_review"] is True  # nothing placed it
    assert (body["category_key"], body["subcategory_key"]) == ("others", "uncategorized")
    row = _manual_rows(session)[0]
    assert row.dedupe_hash.startswith("manual:")
    assert row.user_id == user.id and row.currency == "USD"
    assert row.classified_by != "user"  # not a hand pin: a rule or the cache may still refine it


def test_create_uses_a_matching_rule_without_teaching_the_cache(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    from personal_finance_analyzer.api.services import crud
    from personal_finance_analyzer.db.models import MerchantMap

    statement_id, *_ = import_card(session, tmp_path, "a")
    category = crud.category_by_key(session, "fuel")
    assert category is not None
    sub = crud.subcategory_by_key(session, category.id, "gasoline")
    assert sub is not None
    session.add(
        Rule(
            match_type="contains",
            pattern="zzqx",
            category_id=category.id,
            subcategory_id=sub.id,
            source="user",
        )
    )
    session.commit()
    body = add_manual(client, statement_id, description="Zzqx beans")
    assert (body["category_key"], body["subcategory_key"]) == ("fuel", "gasoline")
    assert body["needs_review"] is False
    assert session.scalars(select(MerchantMap)).all() == []


def test_create_with_a_category_is_a_one_time_edit(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    body = add_manual(
        client, statement_id, category_key="travel", subcategory_key="hotels", kind="purchase"
    )
    assert (body["category_key"], body["subcategory_key"]) == ("travel", "hotels")
    assert _manual_rows(session)[0].classified_by == "user"


def test_create_errors(client: TestClient, session: Session, tmp_path: Path) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    base = {
        "statement_id": statement_id,
        "posted_date": "2026-01-20",
        "description": "x",
        "amount_minor": 100,
    }

    assert client.post("/api/transactions", json={**base, "statement_id": 9999}).status_code == 404

    pending = Statement(
        file_sha256="9" * 64, original_name="p.pdf", status="awaiting_extractor", ingested_at="now"
    )
    session.add(pending)
    session.commit()
    response = client.post("/api/transactions", json={**base, "statement_id": pending.id})
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "this statement is not imported yet; finish importing it first"
    )

    for patch in (
        {"posted_date": "1969-12-31"},
        {"posted_date": "2999-01-01"},
        {"category_key": "travel"},
        {"subcategory_key": "hotels"},
        {"category_key": "nope", "subcategory_key": "hotels"},
        {"category_key": "travel", "subcategory_key": "nope"},
    ):
        assert client.post("/api/transactions", json={**base, **patch}).status_code == 400, patch
    for patch in (
        {"description": "   "},
        {"description": "x" * 201},
        {"description": "bad\u0007char"},
        {"amount_minor": 0},
        {"amount_minor": 10**12},
    ):
        assert client.post("/api/transactions", json={**base, **patch}).status_code == 422, patch
    assert _manual_rows(session) == []


def test_delete_a_manual_row_but_never_a_parsed_one(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    manual_id = int(add_manual(client, statement_id)["id"])  # type: ignore[call-overload]
    assert client.delete(f"/api/transactions/{manual_id}").status_code == 204
    assert _manual_rows(session) == []
    assert client.delete(f"/api/transactions/{manual_id}").status_code == 404

    parsed = rows_of(session, statement_id)["COFFEE SHOP"]
    response = client.delete(f"/api/transactions/{parsed.id}")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "parsed_row"
    assert "COFFEE SHOP" in rows_of(session, statement_id)


def test_a_manual_row_survives_reparse_even_when_edits_are_discarded(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    add_manual(client, statement_id, category_key="travel", subcategory_key="hotels")
    manual_id = _manual_rows(session)[0].id

    job = reparse_via_api(client, statement_id)
    assert job["absorbed_manual"] == 0
    job = reparse_via_api(client, statement_id, discard_edits=True)
    assert job["absorbed_manual"] == 0

    kept = _manual_rows(session)
    assert [t.id for t in kept] == [manual_id]
    assert kept[0].classified_by == "user"  # a manual row is data, not an edit: not discarded
    assert len(rows_of(session, statement_id)) == len(BASE_ROWS) + 1
    assert statement_of(session, statement_id).txn_count == len(BASE_ROWS)  # parsed rows only


def test_an_overlapping_statement_absorbs_the_manual_row(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, user, issuer, _ = import_card(session, tmp_path, "a")
    add_manual(
        client,
        statement_id,
        date="2026-01-20",
        description="gas",
        amount_minor=4000,
        category_key="travel",
        subcategory_key="hotels",
        notes="mine",
    )
    add_manual(client, statement_id, date="2026-01-21", description="other", amount_minor=700)
    second_id, _u, _i, result = import_card(
        session,
        tmp_path,
        "b",
        rows=[*BASE_ROWS, ("01/20/2026", "GAS STATION 77", "40.00")],
        closing="127.43",
        period="Statement Period: 01/01/2026 to 02/01/2026",
        user=user,
        issuer=issuer,
    )
    assert result.absorbed_manual == 1
    assert result.skipped_duplicates == len(BASE_ROWS)
    gas = rows_of(session, second_id)["GAS STATION 77"]
    assert gas.source == "parsed"
    assert (gas.classified_by, gas.notes) == ("user", "mine")  # the lock and the note moved
    assert gas.description_clean != "gas"  # the bank's text wins
    remaining = _manual_rows(session)
    assert [(t.posted_date, t.amount_minor) for t in remaining] == [("2026-01-21", 700)]


def test_a_reparse_that_now_finds_the_row_absorbs_it(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    # The earlier parse missed the bookstore row; the user typed it in.
    session.delete(rows_of(session, statement_id)["ONLINE BOOKSTORE"])
    session.commit()
    add_manual(
        client,
        statement_id,
        date="2026-01-09",
        description="books",
        amount_minor=2999,
        category_key="travel",
        subcategory_key="hotels",
        notes="gift",
    )

    job = reparse_via_api(client, statement_id)
    assert job["absorbed_manual"] == 1
    assert _manual_rows(session) == []
    found = rows_of(session, statement_id)["ONLINE BOOKSTORE"]
    assert (found.classified_by, found.notes) == ("user", "gift")


def test_a_reparse_that_only_refinds_the_existing_twin_keeps_the_manual_row(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """The coffee case: a second coffee typed in by hand is not swallowed by a re-import that
    merely finds the first one again."""
    statement_id, *_ = import_card(session, tmp_path, "a")
    add_manual(client, statement_id, date="2026-01-05", description="coffee 2", amount_minor=1234)

    job = reparse_via_api(client, statement_id)
    assert job["absorbed_manual"] == 0
    assert len(_manual_rows(session)) == 1
    assert statement_of(session, statement_id).txn_count == len(BASE_ROWS)


def test_manual_rows_move_with_change_person(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    add_manual(client, statement_id)
    bob = User(name="bob")
    session.add(bob)
    session.commit()

    response = client.patch(f"/api/statements/{statement_id}", json={"user_id": bob.id})
    assert response.status_code == 200, response.text
    assert response.json()["rows_moved"] == len(BASE_ROWS)  # parsed rows are re-hashed

    manual = _manual_rows(session)[0]
    account = session.get(Account, manual.account_id)
    assert account is not None and account.user_id == bob.id
    assert manual.user_id == bob.id
    assert manual.dedupe_hash.startswith("manual:")
    assert {t.account_id for t in rows_of(session, statement_id).values()} == {account.id}


def test_a_manual_row_clears_the_no_transactions_note(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    statement = statement_of(session, statement_id)
    for txn in rows_of(session, statement_id).values():
        session.delete(txn)
    statement.parsed_row_count = 0
    statement.txn_count = 0
    session.commit()
    assert "no_transactions" in client.get(f"/api/statements/{statement_id}").json()["attention"]

    add_manual(client, statement_id)
    body = client.get(f"/api/statements/{statement_id}").json()
    assert "no_transactions" not in body["attention"]
    assert body["manual_count"] == 1


def test_manual_rows_are_in_export_and_analytics(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    add_manual(client, statement_id, description="Zzqx hardware", amount_minor=4000)

    csv_text = client.get("/api/export", params={"format": "csv"}).text
    header = csv_text.splitlines()[0].split(",")
    assert header[-2:] == ["source", "excluded"]
    assert any(
        "Zzqx hardware" in line and ",manual,False" in line for line in csv_text.splitlines()
    )
    exported = client.get("/api/export", params={"format": "json", "source": "manual"}).json()
    assert [(r["source"], r["excluded"]) for r in exported] == [("manual", False)]

    summary = client.get("/api/analytics/summary").json()
    assert sum(r["total_minor"] for r in summary) == 8743 + 4000

    only = client.get("/api/transactions", params={"source": "manual"}).json()
    assert only["total"] == 1


def test_a_sign_flip_reparse_does_not_absorb_an_unrelated_hand_row(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """After the flip every rebuilt row has a new (date, amount); the hand-added lunch with the
    same date and (flipped) amount as the coffee is not the coffee."""
    negative = [(d, name, f"-{amount}") for d, name, amount in BASE_ROWS]
    statement_id, *_ = import_card(session, tmp_path, "a", rows=negative, closing="-87.43")
    add_manual(client, statement_id, date="2026-01-05", description="lunch", amount_minor=1234)

    job = reparse_via_api(client, statement_id, flip_sign=True)
    assert job["absorbed_manual"] == 0
    assert [(t.description_clean, t.amount_minor) for t in _manual_rows(session)] == [
        ("lunch", 1234)
    ]
    job = reparse_via_api(client, statement_id, flip_sign=False)
    assert job["absorbed_manual"] == 0
    assert len(_manual_rows(session)) == 1


def test_a_reparse_that_changes_the_dates_does_not_absorb_an_unrelated_hand_row(
    client: TestClient, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from personal_finance_analyzer.ingest import pipeline

    statement_id, *_ = import_card(
        session,
        tmp_path,
        "a",
        rows=[(d.replace("2026", "2025"), n, a) for d, n, a in BASE_ROWS],
        period="Statement Period: 01/01/2025 to 01/31/2025",
    )
    add_manual(client, statement_id, date="2026-01-05", description="lunch", amount_minor=1234)

    real_resolve = pipeline._resolve_parser

    class _YearShifted:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

        def parse(self, doc: object) -> object:
            parsed = self._inner.parse(doc)  # type: ignore[attr-defined]
            return replace(
                parsed,
                transactions=tuple(
                    replace(t, posted_date=t.posted_date.replace(year=2026))
                    for t in parsed.transactions
                ),
            )

    monkeypatch.setattr(
        pipeline, "_resolve_parser", lambda *a, **k: _YearShifted(real_resolve(*a, **k))
    )
    job = reparse_via_api(client, statement_id)
    assert job["absorbed_manual"] == 0
    assert len(_manual_rows(session)) == 1
    assert rows_of(session, statement_id)["COFFEE SHOP"].posted_date == "2026-01-05"


def test_a_manual_rows_kind_is_not_overridden_by_a_rule_kind_override(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    from personal_finance_analyzer.api.services import crud

    statement_id, *_ = import_card(session, tmp_path, "a")
    category = crud.category_by_key(session, "fuel")
    assert category is not None
    sub = crud.subcategory_by_key(session, category.id, "gasoline")
    assert sub is not None
    session.add(
        Rule(
            source="user",
            enabled=True,
            priority=1,
            match_type="contains",
            pattern="zzqx",
            category_id=category.id,
            subcategory_id=sub.id,
            kind_override="refund",
            user_id=None,
        )
    )
    session.commit()
    body = add_manual(client, statement_id, kind="purchase")
    assert body["kind"] == "purchase"
    assert _manual_rows(session)[0].kind == "purchase"


def test_reassign_sets_the_target_currency_on_moved_and_manual_rows(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, user, issuer, _ = import_card(session, tmp_path, "a")
    add_manual(client, statement_id)
    target = Account(
        user_id=user.id,
        issuer_id=issuer.id,
        account_type="credit",
        display_name="Euro card",
        mask="9999",
        currency="EUR",
    )
    session.add(target)
    session.commit()
    response = client.patch(f"/api/statements/{statement_id}", json={"account_id": target.id})
    assert response.status_code == 200, response.text
    session.expire_all()
    rows = session.scalars(select(Transaction).where(Transaction.statement_id == statement_id))
    assert {t.currency for t in rows} == {"EUR"}


def test_a_period_change_with_the_current_owner_applies_the_period(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, user, *_ = import_card(session, tmp_path, "a")
    response = client.patch(
        f"/api/statements/{statement_id}",
        json={"user_id": user.id, "period_start": "2026-01-02", "period_end": "2026-02-01"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["period_corrected"] is True
    assert (
        client.patch(f"/api/statements/{statement_id}", json={"user_id": user.id}).status_code
        == 400
    )


def test_a_reparse_that_resolves_another_account_moves_the_manual_rows(
    client: TestClient, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from personal_finance_analyzer.ingest import pipeline

    statement_id, *_ = import_card(session, tmp_path, "a")
    add_manual(client, statement_id)
    real_resolve = pipeline._resolve_parser

    class _OtherMask:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

        def parse(self, doc: object) -> object:
            parsed = self._inner.parse(doc)  # type: ignore[attr-defined]
            return replace(parsed, account_hint=replace(parsed.account_hint, mask="7777"))

    monkeypatch.setattr(
        pipeline, "_resolve_parser", lambda *a, **k: _OtherMask(real_resolve(*a, **k))
    )
    reparse_via_api(client, statement_id)
    new_account_id = statement_of(session, statement_id).account_id
    manual = _manual_rows(session)[0]
    assert manual.account_id == new_account_id
    assert {t.account_id for t in rows_of(session, statement_id).values()} == {new_account_id}
