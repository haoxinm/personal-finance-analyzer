"""Correcting a bad import through the API: statements that parsed to nothing, a safe validated
re-import, the extractor list, changing the person, and re-proposing a pending statement.

Uses real folder-scan imports of the Layout B fixture. "Bank B" has an approved spec whose date
column is off the page (it "parses" cleanly but finds no rows) and which wins the score."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.core.paths import documents_dir
from personal_finance_analyzer.db.models import (
    Account,
    Issuer,
    LayoutSpec,
    Statement,
    Transaction,
    User,
)
from personal_finance_analyzer.ingest.pipeline import NO_TRANSACTIONS_WARNING
from tests.watch.support import (
    add_layout_b_bank,
    add_layout_b_spec,
    add_person,
    put_layout_b,
    put_pdf_bytes,
)

REL = "Alex/Statements/Bank B/jan.pdf"


@pytest.fixture
def alex(session: Session) -> User:
    return add_person(session, "Alex", default=True)


@pytest.fixture
def bank(session: Session) -> Issuer:
    return add_layout_b_bank(session)


@pytest.fixture
def scan(client: TestClient, app_instance: FastAPI) -> Callable[[], None]:
    app_instance.state.watch.scanner._sleep = lambda _s: None

    def run() -> None:
        assert client.post("/api/watch/scan").status_code == 202
        assert app_instance.state.watch.wait_idle(30)

    return run


def _wait(client: TestClient, job_id: str) -> dict[str, Any]:
    job: dict[str, Any] = {}
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {job}")


def _reparse(client: TestClient, statement_id: int, **body: Any) -> dict[str, Any]:
    response = client.post(f"/api/statements/{statement_id}/reparse", json=body)
    assert response.status_code == 202, response.text
    return _wait(client, response.json()["job_id"])


def _rows(session: Session, statement_id: int) -> list[Transaction]:
    session.expire_all()
    return list(
        session.scalars(select(Transaction).where(Transaction.statement_id == statement_id))
    )


@pytest.fixture
def empty(
    client: TestClient, session: Session, alex: User, bank: Issuer, scan: Callable[[], None]
) -> int:
    """A parsed statement that has no transactions (the wrong spec was proposed and used)."""
    add_layout_b_spec(session, bank, wrong=True, name="Wrong B")
    put_layout_b(REL)
    scan()
    statement = session.scalars(select(Statement)).one()
    assert statement.status == "parsed"
    return statement.id


@pytest.fixture
def full(client: TestClient, empty: int, session: Session) -> int:
    """The same statement after a re-import with the built-in parser: it has rows."""
    assert _reparse(client, empty, parser_id="layout_b_credit")["status"] == "done"
    assert _rows(session, empty)
    return empty


# ---------------------------------------------------------------------------- BE-1: attention


def test_a_statement_with_no_rows_needs_attention(client: TestClient, empty: int) -> None:
    body = client.get(f"/api/statements/{empty}").json()
    assert body["status"] == "parsed" and body["txn_count"] == 0
    assert body["parsed_row_count"] == 0
    assert "no_transactions" in body["attention"]
    assert "all_duplicates" not in body["attention"]
    assert NO_TRANSACTIONS_WARNING in body["shape_warnings"]
    assert body["currency"] == "USD"
    assert body["pdf_missing"] is False


def test_a_good_statement_has_no_attention(client: TestClient, full: int) -> None:
    body = client.get(f"/api/statements/{full}").json()
    assert body["parsed_row_count"] == body["txn_count"] > 0
    assert body["attention"] == []


def test_a_fully_overlapping_statement_is_all_duplicates_not_empty(
    client: TestClient, session: Session, alex: User, bank: Issuer, scan: Callable[[], None]
) -> None:
    first = put_layout_b(REL)
    scan()
    # Different bytes (a different file hash), the same transactions.
    put_pdf_bytes("Alex/Statements/Bank B/copy.pdf", first.read_bytes() + b"\n% copy\n")
    scan()
    session.expire_all()
    original, copy = session.scalars(select(Statement).order_by(Statement.id)).all()
    assert (original.txn_count or 0) > 0
    body = client.get(f"/api/statements/{copy.id}").json()
    assert body["txn_count"] == 0 and body["parsed_row_count"] == original.txn_count
    assert "all_duplicates" in body["attention"]
    assert "no_transactions" not in body["attention"]


# ------------------------------------------------------------------------ BE-3: safe re-import


def test_a_reimport_that_finds_nothing_is_refused_and_changes_nothing(
    client: TestClient, session: Session, full: int
) -> None:
    rows = _rows(session, full)
    target = rows[0]
    assert (
        client.patch(
            f"/api/transactions/{target.id}",
            json={"category_key": "travel", "subcategory_key": "hotels"},
        ).status_code
        == 200
    )
    wrong = session.scalars(select(LayoutSpec)).one()

    job = _reparse(client, full, layout_spec_id=wrong.id)
    assert job["status"] == "error" and job["error_code"] == "empty_result"
    assert f"Your {len(rows)} transactions are kept" in job["error_detail"]
    after = _rows(session, full)
    assert sorted(r.id for r in after) == sorted(r.id for r in rows)  # not even rebuilt
    assert [r.classified_by for r in after if r.id == target.id] == ["user"]
    assert session.get(Statement, full).layout_spec_id is None  # type: ignore[union-attr]


def test_allow_empty_lets_a_reimport_remove_every_row(
    client: TestClient, session: Session, full: int
) -> None:
    count = len(_rows(session, full))
    wrong = session.scalars(select(LayoutSpec)).one()
    job = _reparse(client, full, layout_spec_id=wrong.id, allow_empty=True)
    assert job["status"] == "done", job
    assert job["inserted"] == 0 and job["parsed_count"] == 0
    assert job["previous_txn_count"] == count
    assert NO_TRANSACTIONS_WARNING in (job["warnings"] or [])
    assert job["layout_drift"] is True
    assert _rows(session, full) == []
    assert "no_transactions" in client.get(f"/api/statements/{full}").json()["attention"]


def test_a_statement_that_already_had_no_rows_can_be_reimported_to_none_again(
    client: TestClient, empty: int
) -> None:
    job = _reparse(client, empty)
    assert job["status"] == "done", job
    assert (job["inserted"], job["previous_txn_count"]) == (0, 0)


def test_the_reimport_job_reports_counts_and_warnings(
    client: TestClient, session: Session, empty: int
) -> None:
    job = _reparse(client, empty, parser_id="layout_b_credit")
    assert job["status"] == "done", job
    assert job["inserted"] == job["parsed_count"] > 0
    assert job["previous_txn_count"] == 0
    assert job["warnings"] == [] and job["layout_drift"] is False
    assert job["error_code"] is None


def test_reparse_is_validated_before_a_job_is_queued(
    client: TestClient, session: Session, empty: int
) -> None:
    url = f"/api/statements/{empty}/reparse"
    assert client.post(url, json={"parser_id": "layout_b_credit", "pages": []}).status_code == 400
    assert client.post(url, json={"parser_id": "layout_b_credit", "pages": [99]}).status_code == 400
    assert client.post(url, json={"parser_id": "nope"}).status_code == 400
    assert client.post(url, json={"layout_spec_id": 9999}).status_code == 400
    unapproved = add_layout_b_spec(
        session, session.scalars(select(Issuer)).one(), approved=False, name="Draft B"
    )
    assert client.post(url, json={"layout_spec_id": unapproved.id}).status_code == 400
    ok = client.post(url, json={"parser_id": "layout_b_credit", "pages": [1]})
    assert ok.status_code == 202
    _wait(client, ok.json()["job_id"])


def test_a_pdf_that_is_gone_is_409_and_flagged(
    client: TestClient, session: Session, empty: int
) -> None:
    (documents_dir() / REL).unlink()
    listed = client.get("/api/statements").json()
    assert next(s for s in listed if s["id"] == empty)["pdf_missing"] is True
    assert "pdf_missing" in next(s for s in listed if s["id"] == empty)["attention"]
    assert client.get(f"/api/statements/{empty}").json()["has_pdf"] is True
    response = client.post(f"/api/statements/{empty}/reparse", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "pdf_not_found"


def test_a_pdf_that_was_moved_is_still_found(
    client: TestClient, session: Session, empty: int
) -> None:
    moved = documents_dir() / "Alex" / "Statements" / "Bank B" / "2026" / "january.pdf"
    moved.parent.mkdir(parents=True)
    (documents_dir() / REL).rename(moved)
    response = client.post(
        f"/api/statements/{empty}/reparse", json={"parser_id": "layout_b_credit"}
    )
    assert response.status_code == 202
    assert _wait(client, response.json()["job_id"])["status"] == "done"


# ---------------------------------------------------------------------------- BE-3: extractors


def test_the_extractor_list_offers_every_choice_scored(
    client: TestClient, session: Session, empty: int
) -> None:
    response = client.get(f"/api/statements/{empty}/extractors")
    assert response.status_code == 200, response.text
    candidates = response.json()
    first = candidates[0]
    assert (first["kind"], first["recorded"]) == ("spec", True)  # the recorded one is first
    assert first["label"] == "Wrong B (v1)" and first["layout_spec_id"] is not None
    builtin = next(c for c in candidates if c["parser_id"] == "layout_b_credit")
    assert builtin["kind"] == "parser" and builtin["score"] > 0.8 and builtin["recorded"] is False
    assert builtin["label"] == "Layout B credit"
    assert any(c["parser_id"] == "generic_table" for c in candidates)
    assert all(c["kind"] != "llm" for c in candidates)  # not configured
    # After the recorded one, best match first.
    scores = [c["score"] for c in candidates[1:]]
    assert scores == sorted(scores, reverse=True)


def test_only_approved_specs_of_the_issuer_are_listed(
    client: TestClient, session: Session, empty: int
) -> None:
    issuer = session.scalars(select(Issuer)).one()
    add_layout_b_spec(session, issuer, approved=False, name="Draft B")
    other = add_layout_b_bank(session, "Other Bank")
    add_layout_b_spec(session, other, name="Other B")
    labels = [c["label"] for c in client.get(f"/api/statements/{empty}/extractors").json()]
    assert "Wrong B (v1)" in labels
    assert "Draft B (v1)" not in labels and "Other B (v1)" not in labels


def test_the_llm_is_listed_when_configured(
    client: TestClient, empty: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PFA_LLM__ENABLED", "true")
    monkeypatch.setenv("PFA_LLM__BASE_URL", "https://api.example.com")
    monkeypatch.setenv("PFA_LLM__MODEL", "m")
    llm = next(
        c for c in client.get(f"/api/statements/{empty}/extractors").json() if c["kind"] == "llm"
    )
    assert llm["parser_id"] == "llm" and llm["score"] is None


def test_extractors_need_the_pdf(client: TestClient, empty: int) -> None:
    (documents_dir() / REL).unlink()
    assert client.get(f"/api/statements/{empty}/extractors").status_code == 404
    assert client.get("/api/statements/9999/extractors").status_code == 404


# ------------------------------------------------------------------------ BE-4: change person


def test_patching_the_person_moves_the_statement_and_its_rows(
    client: TestClient, session: Session, full: int
) -> None:
    sam = add_person(session, "Sam")
    count = len(_rows(session, full))
    response = client.patch(f"/api/statements/{full}", json={"user_id": sam.id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["user_id"], body["rows_moved"], body["duplicates_deleted"]) == (sam.id, count, 0)
    session.expire_all()
    statement = session.get(Statement, full)
    assert statement is not None and statement.user_id == sam.id
    account = session.get(Account, statement.account_id)
    assert account is not None and account.user_id == sam.id
    assert all(r.user_id == sam.id and r.account_id == account.id for r in _rows(session, full))
    mine = [s["id"] for s in client.get(f"/api/statements?user_id={sam.id}").json()]
    assert full in mine


def test_moving_onto_an_account_that_has_the_same_rows_merges_the_duplicates(
    client: TestClient, session: Session, full: int, scan: Callable[[], None]
) -> None:
    sam = add_person(session, "Sam")
    original = documents_dir() / REL
    put_pdf_bytes("Sam/Statements/Bank B/copy.pdf", original.read_bytes() + b"\n% copy\n")
    scan()
    session.expire_all()
    copy = session.scalars(select(Statement).order_by(Statement.id.desc())).first()
    assert copy is not None and copy.id != full and copy.user_id == sam.id
    count = len(_rows(session, full))
    assert count > 0
    # (the wrong spec was proposed for the copy too: give it its rows with the built-in parser)
    assert _reparse(client, copy.id, parser_id="layout_b_credit")["status"] == "done"
    body = client.patch(f"/api/statements/{full}", json={"user_id": sam.id}).json()
    assert (body["rows_moved"], body["duplicates_deleted"]) == (0, count)
    assert _rows(session, full) == []


def test_patching_an_account_moves_to_its_owner(
    client: TestClient, session: Session, full: int
) -> None:
    sam = add_person(session, "Sam")
    statement = session.get(Statement, full)
    assert statement is not None and statement.account_id is not None
    mine = session.get(Account, statement.account_id)
    assert mine is not None
    target = Account(
        user_id=sam.id,
        issuer_id=mine.issuer_id,
        account_type=mine.account_type,
        display_name="Sam's card",
        mask=mine.mask,
    )
    session.add(target)
    session.commit()
    body = client.patch(f"/api/statements/{full}", json={"account_id": target.id}).json()
    assert body["user_id"] == sam.id and body["account_id"] == target.id and body["rows_moved"] > 0
    session.expire_all()
    assert all(r.account_id == target.id for r in _rows(session, full))


def test_a_patch_that_changes_nothing_is_400(
    client: TestClient, session: Session, alex: User, full: int
) -> None:
    url = f"/api/statements/{full}"
    assert client.patch(url, json={}).status_code == 400
    assert client.patch(url, json={"user_id": alex.id}).status_code == 400
    statement = session.get(Statement, full)
    assert statement is not None
    assert client.patch(url, json={"account_id": statement.account_id}).status_code == 400
    assert client.patch(url, json={"user_id": 9999}).status_code == 404
    assert client.patch("/api/statements/9999", json={"user_id": alex.id}).status_code == 404


def test_the_list_of_a_person_includes_their_unfinished_statements_only(
    client: TestClient, session: Session, alex: User, bank: Issuer
) -> None:
    from personal_finance_analyzer.ingest import pipeline
    from tests.ingest.tax_support import generated_pdf

    sam = add_person(session, "Sam")
    proposal = pipeline.propose_import(
        session, generated_pdf("layout_b_credit", "normal"), user_id=alex.id, original_name="b.pdf"
    )
    session.commit()
    mine = [s["id"] for s in client.get(f"/api/statements?user_id={alex.id}").json()]
    theirs = [s["id"] for s in client.get(f"/api/statements?user_id={sam.id}").json()]
    assert proposal.statement_id in mine and proposal.statement_id not in theirs
    body = client.get(f"/api/statements/{proposal.statement_id}").json()
    assert body["user_id"] == alex.id and body["status"] == "awaiting_extractor"
    # Changing the person of an unfinished statement just changes who it is imported for.
    moved = client.patch(f"/api/statements/{proposal.statement_id}", json={"user_id": sam.id})
    assert moved.status_code == 200 and moved.json()["user_id"] == sam.id
    assert moved.json()["rows_moved"] == 0


def test_a_statement_filed_under_the_wrong_person_can_be_moved_and_retried(
    client: TestClient, session: Session, full: int, scan: Callable[[], None]
) -> None:
    sam = add_person(session, "Sam")
    rel = "Sam/Statements/Bank B/jan.pdf"
    (documents_dir() / rel).parent.mkdir(parents=True)
    (documents_dir() / REL).rename(documents_dir() / rel)
    scan()
    problems = client.get("/api/watch/files", params={"view": "problems"}).json()["items"]
    problem = next(p for p in problems if p["code"] == "duplicate_other_person")
    assert "Change person" in problem["hint"]
    assert client.patch(f"/api/statements/{full}", json={"user_id": sam.id}).status_code == 200
    assert client.post(f"/api/watch/files/{problem['id']}/retry").status_code == 204
    scan()
    recent = client.get("/api/watch/files", params={"view": "recent"}).json()["items"]
    assert {f["path"]: f["outcome"] for f in recent}[rel] == "known"
    assert client.get("/api/watch/files", params={"view": "problems"}).json()["items"] == []


# ------------------------------------------------------------------ BE-2: Retry of 0-row records


def test_retry_of_a_no_transactions_problem_is_409(
    client: TestClient, session: Session, empty: int
) -> None:
    from personal_finance_analyzer.db.models import WatchedFile

    record = session.scalars(select(WatchedFile)).one()
    assert record.code == "no_transactions" and record.statement_id == empty
    response = client.post(f"/api/watch/files/{record.id}/retry")
    assert response.status_code == 409
    assert "Re-import" in response.json()["detail"]


# ------------------------------------------------------------------------ BE-5: /propose


def test_propose_rescores_a_pending_statement_and_refuses_a_parsed_one(
    client: TestClient, session: Session, alex: User, empty: int
) -> None:
    from personal_finance_analyzer.ingest import pipeline
    from tests.ingest.tax_support import generated_pdf

    issuer = session.scalars(select(Issuer)).one()
    spec = session.scalars(select(LayoutSpec)).one()
    pending = pipeline.propose_import(
        session,
        generated_pdf("layout_b_credit", "multiline"),
        user_id=alex.id,
        original_name="m.pdf",
    )
    session.commit()
    issuer.default_spec_id = None
    issuer.default_parser_id = "layout_b_credit"  # remembered; the wrong spec would score higher
    session.commit()
    response = client.post(f"/api/statements/{pending.statement_id}/propose")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["proposal"]["parser_id"] == "layout_b_credit"
    assert body["proposal"]["remembered"] is True and body["proposal"]["layout_spec_id"] is None
    assert body["statement"]["parser_id"] == "layout_b_credit"
    assert body["statement"]["layout_spec_id"] is None
    assert spec.id  # (the wrong spec exists and would have been proposed without the memory)

    assert client.post(f"/api/statements/{empty}/propose").status_code == 409
    assert client.post("/api/statements/9999/propose").status_code == 404


def test_the_issuer_exposes_its_remembered_parser(
    client: TestClient, session: Session, bank: Issuer
) -> None:
    bank.default_parser_id = "layout_b_credit"
    session.commit()
    issuers = client.get("/api/issuers").json()
    assert next(i for i in issuers if i["id"] == bank.id)["default_parser_id"] == "layout_b_credit"
