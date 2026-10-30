"""`/api/statements/*`. `propose_import`/`confirm_import`/`reassign` are faked per the P2-C plan
section ("Route tests use fakes of the §3.12a interfaces"); `ingest/pipeline.py` (P2-A) lands in a
separate branch."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from spend_analyzer.api.services import gateway
from spend_analyzer.db.models import Statement, utcnow_iso


def _make_statement(session: Session, *, status: str = "awaiting_extractor") -> int:
    statement = Statement(
        file_sha256="a" * 64,
        original_name="s.pdf",
        status=status,
        ingested_at=utcnow_iso(),
    )
    session.add(statement)
    session.commit()
    return statement.id


def test_upload_rejects_non_pdf(client: TestClient) -> None:
    response = client.post(
        "/api/statements",
        data={"user_id": "1"},
        files={"file": ("s.pdf", b"not a pdf", "application/pdf")},
    )
    assert response.status_code == 400


def test_upload_rejects_oversized_file(client: TestClient) -> None:
    oversized = b"%PDF-1.4" + b"0" * (26 * 1024 * 1024)
    response = client.post(
        "/api/statements",
        data={"user_id": "1"},
        files={"file": ("s.pdf", oversized, "application/pdf")},
    )
    assert response.status_code == 400


def test_upload_calls_propose_import(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement_id = _make_statement(session)

    def fake_propose_import(
        db_session: Session, pdf_path: Path, *, user_id: int, original_name: str
    ) -> gateway.ImportProposal:
        assert pdf_path.read_bytes().startswith(b"%PDF-")
        assert original_name == "statement.pdf"
        return gateway.ImportProposal(
            statement_id=statement_id,
            status="awaiting_extractor",
            issuer_id=None,
            parser_id="layout_a_credit",
            layout_spec_id=None,
            detect_score=0.9,
            confident=True,
        )

    monkeypatch.setattr(gateway, "propose_import", fake_propose_import)

    response = client.post(
        "/api/statements",
        data={"user_id": "1"},
        files={"file": ("statement.pdf", b"%PDF-1.4 fake pdf bytes", "application/pdf")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["statement"]["id"] == statement_id
    assert body["proposal"]["parser_id"] == "layout_a_credit"
    assert body["duplicate"] is False


def test_upload_flags_an_already_imported_file_as_duplicate(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement_id = _make_statement(session, status="parsed")

    def fake_propose_import(
        db_session: Session, pdf_path: Path, *, user_id: int, original_name: str
    ) -> gateway.ImportProposal:
        return gateway.ImportProposal(
            statement_id=statement_id,
            status="duplicate",
            issuer_id=None,
            parser_id=None,
            layout_spec_id=None,
            detect_score=None,
            confident=False,
        )

    monkeypatch.setattr(gateway, "propose_import", fake_propose_import)

    response = client.post(
        "/api/statements",
        data={"user_id": "1"},
        files={"file": ("statement.pdf", b"%PDF-1.4 fake pdf bytes", "application/pdf")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["duplicate"] is True
    assert body["statement"]["status"] == "parsed"


def test_extract_enqueues_import_job(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement_id = _make_statement(session)

    def fake_confirm_import(
        db_session: Session,
        stmt_id: int,
        *,
        issuer_id: int,
        parser_id: str | None,
        layout_spec_id: int | None,
        remember: bool,
        pages: list[int] | None = None,
    ) -> gateway.ImportResult:
        return gateway.ImportResult(
            statement_id=stmt_id,
            inserted=3,
            skipped_duplicates=2,
            transaction_ids=(),
            reconciliation_delta_minor=0,
            layout_drift=False,
            warnings=(),
        )

    monkeypatch.setattr(gateway, "confirm_import", fake_confirm_import)

    response = client.post(
        f"/api/statements/{statement_id}/extract",
        json={"issuer_id": 1, "remember": False},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    job = _wait_for_job(client, job_id)
    assert job["status"] == "done"
    # The import job reports how many rows were new and how many were already present.
    assert (job["inserted"], job["skipped_duplicates"]) == (3, 2)


def test_reassign_calls_gateway(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement_id = _make_statement(session, status="parsed")
    calls: list[tuple[int, int]] = []

    def fake_reassign(
        db_session: Session, stmt_id: int, *, user_id: int, account_id: int
    ) -> tuple[int, int]:
        calls.append((user_id, account_id))
        return (2, 0)

    monkeypatch.setattr(gateway, "reassign", fake_reassign)

    response = client.patch(f"/api/statements/{statement_id}", json={"user_id": 1, "account_id": 5})
    assert response.status_code == 200
    assert calls == [(1, 5)]


def _wait_for_job(client: TestClient, job_id: str, *, attempts: int = 50) -> dict:
    import time

    for _ in range(attempts):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in time")
