"""Gate 2 HTTP flow: upload a real fixture -> confirm (extract) -> classify (LLM `mode='none'`,
D2's graceful fallback) -> `GET /api/analytics/summary`, and check the totals against the same
hand-computed goldens the CLI end-to-end test (`tests/test_cli.py`) checks against.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from personal_finance_analyzer.classify.kinds import resolve_kind
from personal_finance_analyzer.db.models import Job

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "generated" / "layout_b_credit"
_FIXTURE_PDF = _FIXTURE_DIR / "layout_b_credit_normal.pdf"
_FIXTURE_JSON = _FIXTURE_DIR / "layout_b_credit_normal.json"

#: The fixture's own page-1 header text (see `layout_b_credit.py`'s parser docstring / the PDF
#: itself), registered as an issuer so `issuer_match.match_issuer` matches it by name (A24).
_ISSUER_NAME = "CASCADE TRUST BANK"


def _expected_total() -> int:
    """Purchases/fees/interest/refunds only (payments/transfers excluded), computed exactly as
    the ingest pipeline resolves `Kind` at import time (I11/A7) — mirrors
    `tests.test_cli._expected_report_total`'s per-fixture logic for this one fixture."""
    golden = json.loads(_FIXTURE_JSON.read_text())
    total = 0
    for txn in golden["transactions"]:
        kind = resolve_kind(
            kind_hint=txn["kind_hint"],
            description_clean=txn["description"],
            amount_minor=txn["amount_minor"],
            account_type="credit",
        )
        if kind in ("purchase", "fee", "interest", "refund"):
            total += txn["amount_minor"]
    return total


def _wait_for_job(session_factory: object, job_id: str, *, timeout: float = 10.0) -> Job:
    deadline = time.time() + timeout
    factory = session_factory  # sessionmaker[Session], typed loosely to avoid importing it here
    with factory() as session:  # type: ignore[operator]
        job = session.get(Job, job_id)
        while job is not None and job.status not in ("done", "error") and time.time() < deadline:
            session.expire(job)
            job = session.get(Job, job_id)
            time.sleep(0.02)
        assert job is not None, f"job {job_id} vanished"
        assert job.status == "done", f"job {job_id} ended in {job.status}: {job.error_detail}"
        return job


def _latest_job_id(session: Session, *, kind: str) -> str:
    from sqlalchemy import select

    job_id = (
        session.execute(select(Job.id).where(Job.kind == kind).order_by(Job.id.desc()))
        .scalars()
        .first()
    )
    assert job_id is not None, f"no {kind!r} job was ever created"
    return job_id


def test_upload_confirm_classify_matches_analytics_summary(
    app_instance: FastAPI, client: TestClient, session: Session
) -> None:
    user = client.post("/api/users", json={"name": "Alex", "is_default": True}).json()
    issuer = client.post("/api/issuers", json={"name": _ISSUER_NAME, "match_terms": []}).json()

    with _FIXTURE_PDF.open("rb") as fh:
        upload = client.post(
            "/api/statements",
            files={"file": ("layout_b_credit_normal.pdf", fh, "application/pdf")},
            data={"user_id": str(user["id"])},
        )
    assert upload.status_code == 201, upload.text
    body = upload.json()
    statement_id = body["statement"]["id"]
    proposal = body["proposal"]
    assert proposal["issuer_id"] == issuer["id"]  # matched by name (A24), not hand-picked

    extract = client.post(
        f"/api/statements/{statement_id}/extract",
        json={"issuer_id": issuer["id"], "parser_id": proposal["parser_id"]},
    )
    assert extract.status_code == 202, extract.text
    import_job_id = extract.json()["job_id"]

    session_factory = app_instance.state.session_factory
    _wait_for_job(session_factory, import_job_id)

    # The import job enqueues a classify job for the rows it inserted (job_tasks.py); it has no
    # id of its own in the response, so find it directly.
    session.expire_all()
    classify_job_id = _latest_job_id(session, kind="classify")
    _wait_for_job(session_factory, classify_job_id)

    summary = client.get("/api/analytics/summary", params={"user_ids": [user["id"]]})
    assert summary.status_code == 200, summary.text
    rows = summary.json()
    actual_total = sum(row["total_minor"] for row in rows)
    assert actual_total == _expected_total()
