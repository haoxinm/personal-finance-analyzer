"""`POST /api/statements/{id}/reparse`: default extractor, chained classification, discard_edits,
up-front 4xx, and the new Statement fields. Uses a real folder-scan import."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.models import Statement, Transaction, User, utcnow_iso
from tests.watch.support import add_bank, add_person, put_statement

STATEMENT = "Alex/Statements/Fixture Bank/jan.pdf"


@pytest.fixture
def alex(session: Session) -> User:
    add_bank(session)
    return add_person(session, "Alex", default=True)


@pytest.fixture
def imported(client: TestClient, app_instance: FastAPI, alex: User, session: Session) -> int:
    app_instance.state.watch.scanner._sleep = lambda _s: None
    put_statement(STATEMENT)
    assert client.post("/api/watch/scan").status_code == 202
    assert app_instance.state.watch.wait_idle(30)
    statement = session.scalars(select(Statement)).one()
    assert statement.status == "parsed"
    return statement.id


def _wait(client: TestClient, job_id: str) -> dict[str, object]:
    job: dict[str, object] = {}
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {job}")


def _rows(session: Session, statement_id: int) -> list[Transaction]:
    session.expire_all()
    return list(
        session.scalars(
            select(Transaction)
            .where(Transaction.statement_id == statement_id)
            .order_by(Transaction.id)
        )
    )


def test_statement_json_has_name_and_pdf_flags(client: TestClient, imported: int) -> None:
    body = client.get(f"/api/statements/{imported}").json()
    assert body["original_name"] == "jan.pdf"
    assert body["has_pdf"] is True
    assert body["pdf_in_place"] is True


def test_reparse_with_an_empty_body_uses_the_recorded_extractor_and_classifies(
    client: TestClient, session: Session, imported: int
) -> None:
    before = len(_rows(session, imported))
    assert before > 0
    response = client.post(f"/api/statements/{imported}/reparse", json={})
    assert response.status_code == 202
    job = _wait(client, response.json()["job_id"])
    assert job["status"] == "done", job
    assert job["inserted"] == before
    assert job["classify_job_id"] is not None
    classify = _wait(client, str(job["classify_job_id"]))
    assert classify["status"] == "done"
    rows = _rows(session, imported)
    assert len(rows) == before
    # The rebuilt rows went through the cascade: each is classified or flagged for review.
    assert all(r.classified_by is not None or r.needs_review for r in rows)


def test_reparse_keeps_a_one_time_edit_by_default_and_discards_it_on_request(
    client: TestClient, session: Session, imported: int
) -> None:
    target = _rows(session, imported)[0]
    patched = client.patch(
        f"/api/transactions/{target.id}",
        json={"category_key": "travel", "subcategory_key": "hotels"},
    )
    assert patched.status_code == 200, patched.text

    kept = _wait(
        client, client.post(f"/api/statements/{imported}/reparse", json={}).json()["job_id"]
    )
    assert kept["restored_edits"] == 1 and kept["unmatched_edits"] == 0
    assert kept["discarded_edits"] == 0
    _wait(client, str(kept["classify_job_id"]))
    edited = [r for r in _rows(session, imported) if r.classified_by == "user"]
    assert len(edited) == 1

    dropped = _wait(
        client,
        client.post(f"/api/statements/{imported}/reparse", json={"discard_edits": True}).json()[
            "job_id"
        ],
    )
    assert dropped["discarded_edits"] == 1 and dropped["restored_edits"] == 0
    _wait(client, str(dropped["classify_job_id"]))
    assert not [r for r in _rows(session, imported) if r.classified_by == "user"]


def test_reparse_carries_a_field_correction_over(
    client: TestClient, session: Session, imported: int
) -> None:
    target = _rows(session, imported)[0]
    original = (target.posted_date, target.amount_minor, target.description_clean)
    assert (
        client.patch(
            f"/api/transactions/{target.id}",
            json={"amount_minor": original[1] + 100, "description_clean": "Fixed name"},
        ).status_code
        == 200
    )
    job = _wait(
        client, client.post(f"/api/statements/{imported}/reparse", json={}).json()["job_id"]
    )
    assert job["restored_edits"] == 1
    rebuilt = next(r for r in _rows(session, imported) if r.description_clean == "Fixed name")
    assert rebuilt.amount_minor == original[1] + 100
    assert rebuilt.orig_amount_minor == original[1]
    assert rebuilt.orig_description_clean == original[2]
    assert rebuilt.orig_posted_date is None
    assert rebuilt.classified_by != "user"  # a correction alone is not a category lock


def test_reparse_both_extractors_is_400(client: TestClient, imported: int) -> None:
    response = client.post(
        f"/api/statements/{imported}/reparse",
        json={"parser_id": "layout_a_credit", "layout_spec_id": 1},
    )
    assert response.status_code == 400


def test_reparse_without_a_recorded_extractor_is_400(
    client: TestClient, session: Session, imported: int
) -> None:
    statement = session.get(Statement, imported)
    assert statement is not None
    statement.parser_id = None
    statement.layout_spec_id = None
    session.commit()
    response = client.post(f"/api/statements/{imported}/reparse", json={})
    assert response.status_code == 400
    assert "extractor" in response.json()["detail"]


def test_reparse_without_a_pdf_copy_is_409(
    client: TestClient, session: Session, imported: int
) -> None:
    statement = session.get(Statement, imported)
    assert statement is not None
    statement.stored_path = None
    session.commit()
    assert client.get(f"/api/statements/{imported}").json()["has_pdf"] is False
    response = client.post(f"/api/statements/{imported}/reparse", json={})
    assert response.status_code == 409
    assert "PDF" in response.json()["detail"]


def test_reparse_of_an_unconfirmed_statement_is_409(client: TestClient, session: Session) -> None:
    statement = Statement(
        file_sha256="c" * 64,
        original_name="x.pdf",
        status="awaiting_extractor",
        parser_id="generic_table",
        ingested_at=utcnow_iso(),
    )
    session.add(statement)
    session.commit()
    response = client.post(f"/api/statements/{statement.id}/reparse", json={})
    assert response.status_code == 409
    assert client.post("/api/statements/9999/reparse", json={}).status_code == 404


def test_the_layout_mapper_body_is_still_accepted(client: TestClient, imported: int) -> None:
    response = client.post(
        f"/api/statements/{imported}/reparse",
        json={
            "issuer_id": 1,
            "layout_spec_id": None,
            "parser_id": "layout_a_credit",
            "remember": False,
        },
    )
    assert response.status_code == 202
    assert _wait(client, response.json()["job_id"])["status"] == "done"
