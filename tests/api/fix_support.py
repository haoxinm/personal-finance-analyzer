"""Helpers for the manual-row / excluded-row / statement-fix tests: real card statements imported
through the pipeline (generic table parser), so reparse and overlap behave like the app."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.models import Issuer, Statement, Transaction, User
from personal_finance_analyzer.ingest import pipeline
from tests.ingest.test_pipeline import _build_card_pdf, _make_issuer, _make_user

BASE_ROWS = [
    ("01/03/2026", "GROCERY MART", "45.10"),
    ("01/05/2026", "COFFEE SHOP", "12.34"),
    ("01/09/2026", "ONLINE BOOKSTORE", "29.99"),
]


def import_card(
    session: Session,
    tmp_path: Path,
    name: str,
    *,
    rows: list[tuple[str, str, str]] | None = None,
    closing: str = "87.43",
    period: str = "Statement Period: 01/01/2026 to 01/31/2026",
    user: User | None = None,
    issuer: Issuer | None = None,
) -> tuple[int, User, Issuer, pipeline.ImportResult]:
    """Import a generic-table card statement; returns (statement id, user, issuer, result)."""
    pdf_path = tmp_path / f"{name}.pdf"
    _build_card_pdf(pdf_path, BASE_ROWS if rows is None else rows, closing=closing, period=period)
    user = user or _make_user(session, f"user-{name}")
    issuer = issuer or _make_issuer(session, f"{name} Bank")
    proposal = pipeline.propose_import(
        session, pdf_path, user_id=user.id, original_name=f"{name}.pdf"
    )
    result = pipeline.confirm_import(
        session,
        proposal.statement_id,
        issuer_id=issuer.id,
        parser_id="generic_table",
        layout_spec_id=None,
        remember=False,
    )
    return proposal.statement_id, user, issuer, result


def rows_of(session: Session, statement_id: int) -> dict[str, Transaction]:
    """The statement's rows by raw description."""
    session.expire_all()
    return {
        t.description_raw: t
        for t in session.scalars(
            select(Transaction).where(Transaction.statement_id == statement_id)
        )
    }


def statement_of(session: Session, statement_id: int) -> Statement:
    session.expire_all()
    statement = session.get(Statement, statement_id)
    assert statement is not None
    return statement


def add_manual(
    client: TestClient,
    statement_id: int,
    *,
    date: str = "2026-01-20",
    description: str = "Zzqx hardware",
    amount_minor: int = 4000,
    **extra: object,
) -> dict[str, object]:
    response = client.post(
        "/api/transactions",
        json={
            "statement_id": statement_id,
            "posted_date": date,
            "description": description,
            "amount_minor": amount_minor,
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


def wait_job(client: TestClient, job_id: str) -> dict[str, object]:
    job: dict[str, object] = {}
    for _ in range(400):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {job}")


def reparse_via_api(client: TestClient, statement_id: int, **body: object) -> dict[str, object]:
    response = client.post(
        f"/api/statements/{statement_id}/reparse", json={"parser_id": "generic_table", **body}
    )
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["job_id"])
    assert job["status"] == "done", job
    if job.get("classify_job_id"):
        wait_job(client, str(job["classify_job_id"]))
    return job
