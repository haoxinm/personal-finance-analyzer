"""Statement-level fixes: period, account currency, bank (issuer), sign flip."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.models import Account, Issuer, Statement, Transaction
from personal_finance_analyzer.ingest import pipeline
from tests.api.fix_support import (
    BASE_ROWS,
    add_manual,
    import_card,
    reparse_via_api,
    rows_of,
    statement_of,
)

NEGATIVE_ROWS = [(d, name, f"-{amount}") for d, name, amount in BASE_ROWS]


def _patch(client: TestClient, statement_id: int, **body: object) -> object:
    return client.patch(f"/api/statements/{statement_id}", json=body)


# --- period -----------------------------------------------------------------------------------


def test_period_patch_revert_and_parsed_values(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    body = client.get(f"/api/statements/{statement_id}").json()
    assert (body["period_start"], body["period_end"]) == ("2026-01-01", "2026-01-31")
    assert body["period_corrected"] is False
    assert (body["parsed_period_start"], body["parsed_period_end"]) == ("2026-01-01", "2026-01-31")

    response = client.patch(
        f"/api/statements/{statement_id}",
        json={"period_start": "2026-01-02", "period_end": "2026-02-01"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["period_start"], body["period_end"]) == ("2026-01-02", "2026-02-01")
    assert body["period_corrected"] is True
    assert (body["parsed_period_start"], body["parsed_period_end"]) == ("2026-01-01", "2026-01-31")
    assert (body["rows_moved"], body["duplicates_deleted"]) == (0, 0)

    reverted = client.patch(f"/api/statements/{statement_id}", json={"revert_period": True}).json()
    assert (reverted["period_start"], reverted["period_end"]) == ("2026-01-01", "2026-01-31")
    assert reverted["period_corrected"] is False
    assert (
        client.patch(f"/api/statements/{statement_id}", json={"revert_period": True}).status_code
        == 400
    )


def test_period_validation(client: TestClient, session: Session, tmp_path: Path) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    url = f"/api/statements/{statement_id}"
    for body in (
        {"period_start": "2026-01-02"},
        {"period_end": "2026-01-02"},
        {"period_start": "2026-02-02", "period_end": "2026-01-02"},
        {"period_start": "2025-01-01", "period_end": "2026-03-01"},  # > 400 days
        {"period_start": "1960-01-01", "period_end": "1960-02-01"},
        {"period_start": "2999-01-01", "period_end": "2999-02-01"},
        {"revert_period": True, "period_start": "2026-01-02", "period_end": "2026-01-03"},
        {"period_start": "2026-01-01", "period_end": "2026-01-31"},  # what it already is
    ):
        assert client.patch(url, json=body).status_code == 400, body
    pending = Statement(
        file_sha256="8" * 64, original_name="p.pdf", status="awaiting_extractor", ingested_at="now"
    )
    session.add(pending)
    session.commit()
    response = client.patch(
        f"/api/statements/{pending.id}",
        json={"period_start": "2026-01-02", "period_end": "2026-01-03"},
    )
    assert response.status_code == 409


def test_a_corrected_period_survives_reparse_and_clears_when_the_parse_agrees(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    client.patch(
        f"/api/statements/{statement_id}",
        json={"period_start": "2026-01-02", "period_end": "2026-02-01"},
    )
    reparse_via_api(client, statement_id)
    body = client.get(f"/api/statements/{statement_id}").json()
    assert (body["period_start"], body["period_end"]) == ("2026-01-02", "2026-02-01")
    assert body["period_corrected"] is True
    assert (body["parsed_period_start"], body["parsed_period_end"]) == ("2026-01-01", "2026-01-31")

    # The correction equals what the parse now says: it is no correction any more.
    statement = statement_of(session, statement_id)
    statement.period_start, statement.period_end = "2026-01-01", "2026-01-31"
    statement.orig_period_start, statement.orig_period_end = "2025-12-01", "2025-12-31"
    session.commit()
    reparse_via_api(client, statement_id)
    body = client.get(f"/api/statements/{statement_id}").json()
    assert body["period_corrected"] is False
    assert (body["period_start"], body["parsed_period_end"]) == ("2026-01-01", "2026-01-31")
    assert statement_of(session, statement_id).orig_period_start is None


# --- bank (issuer) ----------------------------------------------------------------------------


def test_change_bank_moves_to_a_new_account_and_reparse_uses_it(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, user, old_issuer, _ = import_card(session, tmp_path, "a")
    add_manual(client, statement_id)
    new_issuer = Issuer(name="Better Bank", slug="better-bank")
    session.add(new_issuer)
    session.commit()
    old_account_id = statement_of(session, statement_id).account_id

    response = client.patch(f"/api/statements/{statement_id}", json={"issuer_id": new_issuer.id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["rows_moved"], body["duplicates_deleted"]) == (len(BASE_ROWS), 0)
    assert body["issuer_id"] == new_issuer.id and body["account_id"] != old_account_id
    account = session.get(Account, body["account_id"])
    assert account is not None
    assert (account.issuer_id, account.user_id) == (new_issuer.id, user.id)
    assert {t.account_id for t in rows_of(session, statement_id).values()} == {account.id}

    reparse_via_api(client, statement_id)
    assert statement_of(session, statement_id).account_id == account.id
    assert {t.account_id for t in rows_of(session, statement_id).values()} == {account.id}
    assert old_issuer.id != new_issuer.id


def test_change_bank_merges_duplicates_into_an_existing_account(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, user, _issuer, _ = import_card(session, tmp_path, "a")
    other = Issuer(name="Other Bank", slug="other-bank")
    session.add(other)
    session.commit()
    second_id, *_ = import_card(
        session,
        tmp_path,
        "b",
        closing="87.43",
        period="Statement Period: 01/01/2026 to 01/30/2026",
        user=user,
        issuer=other,
    )
    response = client.patch(f"/api/statements/{statement_id}", json={"issuer_id": other.id})
    assert response.status_code == 200, response.text
    assert response.json()["duplicates_deleted"] == len(BASE_ROWS)
    assert statement_of(session, second_id).account_id == response.json()["account_id"]


def test_change_bank_errors(client: TestClient, session: Session, tmp_path: Path) -> None:
    statement_id, _user, issuer, _ = import_card(session, tmp_path, "a")
    url = f"/api/statements/{statement_id}"
    assert client.patch(url, json={"issuer_id": 9999}).status_code == 404
    assert client.patch(url, json={"issuer_id": issuer.id, "account_id": 1}).status_code == 400
    assert client.patch(url, json={"issuer_id": issuer.id}).status_code == 400  # same bank
    pending = Statement(
        file_sha256="7" * 64, original_name="p.pdf", status="awaiting_extractor", ingested_at="now"
    )
    session.add(pending)
    session.commit()
    assert (
        client.patch(f"/api/statements/{pending.id}", json={"issuer_id": issuer.id}).status_code
        == 409
    )


# --- account currency -------------------------------------------------------------------------


def test_account_currency_rewrites_rows_and_survives_reparse(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = import_card(session, tmp_path, "a")
    account_id = statement_of(session, statement_id).account_id
    assert client.patch(f"/api/accounts/{account_id}", json={"currency": "usd"}).status_code == 400
    assert client.patch(f"/api/accounts/{account_id}", json={"currency": "EURO"}).status_code == 400

    assert client.patch(f"/api/accounts/{account_id}", json={"currency": "EUR"}).status_code == 200
    assert {t.currency for t in rows_of(session, statement_id).values()} == {"EUR"}
    assert client.get("/api/transactions").json()["total"] == 0  # the grid defaults to USD
    assert client.get("/api/transactions", params={"currency": "EUR"}).json()["total"] == 3
    assert sum(r["total_minor"] for r in client.get("/api/analytics/summary").json()) == 0
    eur = client.get("/api/analytics/summary", params={"currency": "EUR"}).json()
    assert sum(r["total_minor"] for r in eur) == 8743
    assert client.get(f"/api/statements/{statement_id}").json()["currency"] == "EUR"

    reparse_via_api(client, statement_id)
    assert {t.currency for t in rows_of(session, statement_id).values()} == {"EUR"}
    add_manual(client, statement_id)
    assert {t.currency for t in rows_of(session, statement_id).values()} == {"EUR"}


def test_updating_an_account_into_a_duplicate_is_a_409(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, user, issuer, _ = import_card(session, tmp_path, "a")
    first = statement_of(session, statement_id).account_id
    second = Account(
        user_id=user.id, issuer_id=issuer.id, account_type="credit", display_name="x", mask="1111"
    )
    session.add(second)
    session.commit()
    response = client.patch(f"/api/accounts/{second.id}", json={"mask": ""})
    assert response.status_code == 409
    assert session.get(Account, second.id).mask == "1111"  # type: ignore[union-attr]
    assert first is not None


# --- sign flip --------------------------------------------------------------------------------


def _signed_import(session: Session, tmp_path: Path) -> tuple[int, int, int]:
    """A card statement the extractor read with every sign wrong (it printed -45.10 ...)."""
    statement_id, user, issuer, _ = import_card(session, tmp_path, "neg", rows=NEGATIVE_ROWS)
    return statement_id, user.id, issuer.id


def test_sign_flip_is_suggested_and_a_flipped_reimport_reconciles(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = _signed_import(session, tmp_path)
    body = client.get(f"/api/statements/{statement_id}").json()
    assert body["reconciliation_delta_minor"] == -17486
    assert body["sign_flip_suggested"] is True and body["flip_sign"] is False
    assert all(t.amount_minor < 0 for t in rows_of(session, statement_id).values())

    reparse_via_api(client, statement_id, flip_sign=True)
    rows = rows_of(session, statement_id)
    assert sorted(t.amount_minor for t in rows.values()) == [1234, 2999, 4510]
    assert {t.kind for t in rows.values()} == {"purchase"}
    body = client.get(f"/api/statements/{statement_id}").json()
    assert body["flip_sign"] is True and body["reconciliation_delta_minor"] == 0
    assert body["sign_flip_suggested"] is False
    assert "reconciliation_mismatch" not in body["attention"]

    # Persisted: a re-import that says nothing keeps it; flip_sign=false puts it back.
    reparse_via_api(client, statement_id)
    assert statement_of(session, statement_id).flip_sign is True
    assert min(t.amount_minor for t in rows_of(session, statement_id).values()) > 0
    reparse_via_api(client, statement_id, flip_sign=False)
    assert statement_of(session, statement_id).flip_sign is False
    assert max(t.amount_minor for t in rows_of(session, statement_id).values()) < 0


def test_flip_carries_locks_corrections_and_exclusions_and_leaves_manual_rows(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = _signed_import(session, tmp_path)
    rows = rows_of(session, statement_id)
    coffee, grocery = rows["COFFEE SHOP"], rows["GROCERY MART"]
    assert (
        client.patch(
            f"/api/transactions/{coffee.id}",
            json={"category_key": "travel", "subcategory_key": "hotels", "amount_minor": 1234},
        ).status_code
        == 200
    )
    client.post(
        "/api/transactions/bulk-exclude", json={"transaction_ids": [grocery.id], "excluded": True}
    )
    manual = add_manual(client, statement_id, amount_minor=-500)

    job = reparse_via_api(client, statement_id, flip_sign=True)
    assert (job["restored_edits"], job["unmatched_edits"]) == (2, 0)
    rows = rows_of(session, statement_id)
    flipped = rows["COFFEE SHOP"]
    assert (
        flipped.amount_minor == 1234 and flipped.orig_amount_minor is None
    )  # correction redundant
    assert (flipped.classified_by, flipped.category_id is not None) == ("user", True)
    assert rows["GROCERY MART"].excluded is True and rows["GROCERY MART"].amount_minor == 4510
    still = session.get(Transaction, int(manual["id"]))  # type: ignore[call-overload]
    assert still is not None and still.amount_minor == -500 and still.source == "manual"


def test_an_overlapping_correctly_signed_statement_dedupes_against_the_flipped_rows(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement_id, user_id, issuer_id = _signed_import(session, tmp_path)
    reparse_via_api(client, statement_id, flip_sign=True)
    user = session.get(Account, statement_of(session, statement_id).account_id).user_id  # type: ignore[union-attr]
    assert user == user_id
    from personal_finance_analyzer.db.models import User

    second_id, *_rest, result = import_card(
        session,
        tmp_path,
        "good",
        rows=[*BASE_ROWS, ("01/20/2026", "GAS STATION 77", "40.00")],
        closing="127.43",
        user=session.get(User, user_id),
        issuer=session.get(Issuer, issuer_id),
    )
    assert (result.inserted, result.skipped_duplicates) == (1, 3)
    assert second_id != statement_id
    assert session.scalars(select(Transaction).where(Transaction.statement_id == second_id)).all()


def test_flip_sign_is_applied_by_the_pipeline_before_the_total_is_stored(
    session: Session, tmp_path: Path
) -> None:
    statement_id, *_ = _signed_import(session, tmp_path)
    assert statement_of(session, statement_id).parsed_total_minor == -8743
    result = pipeline.reparse(
        session, statement_id, parser_id="generic_table", layout_spec_id=None, flip_sign=True
    )
    session.commit()
    statement = statement_of(session, statement_id)
    assert statement.parsed_total_minor == 8743 and statement.flip_sign is True
    assert result.reconciliation_delta_minor == 0
