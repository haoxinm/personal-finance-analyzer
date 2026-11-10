"""Page selection endpoints: /pages, /pages/{n}/image, /llm-preview, extract/reparse `pages`."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from personal_finance_analyzer.api.services import gateway
from personal_finance_analyzer.core.paths import statements_dir
from personal_finance_analyzer.db.models import Statement, utcnow_iso
from personal_finance_analyzer.ingest.extract import sha256_file
from tests.fixtures.gen.base import render_lines_pdf


def _staged_statement(session: Session, tmp_path: Path, **fields: object) -> int:
    src = tmp_path / "s.pdf"
    render_lines_pdf(
        src,
        [
            ["Page one header text " * 4, "Card 4111 1111 1111 1111 jane@example.com"],
            ["Transactions for Alice Example " * 3, "01/05 COFFEE 12.34"],
            ["Disclosures " * 8],
        ],
    )
    sha = sha256_file(src)
    statements_dir().mkdir(parents=True, exist_ok=True)
    (statements_dir() / f"{sha}.pdf").write_bytes(src.read_bytes())
    statement = Statement(
        file_sha256=sha,
        original_name="s.pdf",
        status="awaiting_extractor",
        page_count=3,
        ingested_at=utcnow_iso(),
        **fields,
    )
    session.add(statement)
    session.commit()
    return statement.id


def test_pages_listing_and_selection_flags(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    sid = _staged_statement(session, tmp_path, selected_pages="[2]")
    rows = client.get(f"/api/statements/{sid}/pages").json()
    assert [r["page_number"] for r in rows] == [1, 2, 3]
    assert [r["selected"] for r in rows] == [False, True, False]
    assert rows[0]["char_count"] > 0 and "Page one" in rows[0]["snippet"]

    body = client.get(f"/api/statements/{sid}").json()
    assert body["page_count"] == 3 and body["selected_pages"] == [2]


def test_pages_all_selected_by_default_and_404(client: TestClient, session: Session) -> None:
    missing = Statement(
        file_sha256="b" * 64, original_name="x.pdf", status="parsed", ingested_at=utcnow_iso()
    )
    session.add(missing)
    session.commit()
    assert client.get(f"/api/statements/{missing.id}/pages").status_code == 404
    assert client.get(f"/api/statements/{missing.id}/pages/1/image").status_code == 404
    assert client.get("/api/statements/99999/pages").status_code == 404


def test_page_image_png(client: TestClient, session: Session, tmp_path: Path) -> None:
    sid = _staged_statement(session, tmp_path)
    resp = client.get(f"/api/statements/{sid}/pages/1/image?width=200")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "private, max-age=300"
    assert resp.content.startswith(b"\x89PNG\r\n\x1a\n")
    small = client.get(f"/api/statements/{sid}/pages/1/image?width=1")  # clamped to 100
    big = client.get(f"/api/statements/{sid}/pages/1/image?width=999999")  # clamped to 1600
    assert small.status_code == big.status_code == 200
    assert len(big.content) > len(small.content)
    assert client.get(f"/api/statements/{sid}/pages/9/image").status_code == 404
    assert client.get(f"/api/statements/{sid}/pages/0/image").status_code == 404


def test_image_accepts_token_query_param(
    app_instance: object, session: Session, tmp_path: Path
) -> None:
    from fastapi import FastAPI

    assert isinstance(app_instance, FastAPI)
    sid = _staged_statement(session, tmp_path)
    with TestClient(app_instance, base_url="http://127.0.0.1") as bare:
        assert bare.get(f"/api/statements/{sid}/pages/1/image").status_code == 403
        ok = bare.get(f"/api/statements/{sid}/pages/1/image?token=test-token")
        assert ok.status_code == 200


def _configure_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_LLM__ENABLED", "true")
    monkeypatch.setenv("PFA_LLM__BASE_URL", "https://api.example.com")
    monkeypatch.setenv("PFA_LLM__MODEL", "test-model")


def test_llm_preview_redacts_and_honours_pages(
    client: TestClient, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_llm(monkeypatch)
    monkeypatch.setenv("PFA_PRIVACY__PII_TERMS", '["Alice Example"]')
    sid = _staged_statement(session, tmp_path)

    all_pages = client.get(f"/api/statements/{sid}/llm-preview")
    assert all_pages.status_code == 200
    data = all_pages.json()
    assert data["mode"] == "remote" and data["model"] == "test-model"
    assert data["pages"] == [1, 2, 3] and len(data["requests"]) == 1
    text = data["requests"][0]
    assert "XXXX1111" in text and "[email]" in text and "4111" not in text
    assert data["total_chars"] == len(text)

    chosen = client.get(f"/api/statements/{sid}/llm-preview?pages=1,3").json()
    assert chosen["pages"] == [1, 3]
    assert "=== PAGE 2 ===" not in chosen["requests"][0]

    assert client.get(f"/api/statements/{sid}/llm-preview?pages=abc").status_code == 422


def test_llm_preview_reports_user_entry_redactions(
    client: TestClient, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_llm(monkeypatch)
    monkeypatch.setenv("PFA_PRIVACY__PII_TERMS", "Alice Example,4111-1111-1111-1111")
    sid = _staged_statement(session, tmp_path)
    data = client.get(f"/api/statements/{sid}/llm-preview").json()
    text = data["requests"][0]
    assert "Alice" not in text and "[NUMBER]" in text and "XXXX1111" not in text
    assert data["redactions"] == 4  # three names on page 2 + the card number on page 1

    monkeypatch.setenv("PFA_PRIVACY__PII_TERMS", "")
    assert client.get(f"/api/statements/{sid}/llm-preview").json()["redactions"] == 0


def test_llm_preview_uses_saved_selection(
    client: TestClient, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_llm(monkeypatch)
    sid = _staged_statement(session, tmp_path, selected_pages="[2]")
    assert client.get(f"/api/statements/{sid}/llm-preview").json()["pages"] == [2]


def test_extract_and_reparse_pass_pages_through(
    client: TestClient, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _staged_statement(session, tmp_path)
    seen: dict[str, object] = {}

    def fake_confirm(db: Session, statement_id: int, **kw: object) -> gateway.ImportResult:
        seen["confirm"] = kw
        return gateway.ImportResult(statement_id, 0, 0, (), None, False, ())

    def fake_reparse(db: Session, statement_id: int, **kw: object) -> gateway.ImportResult:
        seen["reparse"] = kw
        return gateway.ImportResult(statement_id, 0, 0, (), None, False, ())

    monkeypatch.setattr(gateway, "confirm_import", fake_confirm)
    monkeypatch.setattr(gateway, "reparse", fake_reparse)
    r = client.post(
        f"/api/statements/{sid}/extract",
        json={"issuer_id": 1, "parser_id": "llm", "pages": [1, 3]},
    )
    assert r.status_code == 202
    r = client.post(
        f"/api/statements/{sid}/reparse",
        json={"issuer_id": 1, "parser_id": "llm", "pages": [2]},
    )
    assert r.status_code == 202
    import time

    for _ in range(100):
        if len(seen) == 2:
            break
        time.sleep(0.05)
    assert seen["confirm"]["pages"] == [1, 3]  # type: ignore[index]
    assert seen["confirm"]["parser_id"] == "llm"  # type: ignore[index]
    assert seen["reparse"]["pages"] == [2]  # type: ignore[index]


def test_reparse_job_reports_restored_and_unmatched_edits(
    client: TestClient, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    sid = _staged_statement(session, tmp_path)

    def fake_reparse(db: Session, statement_id: int, **kw: object) -> gateway.ImportResult:
        return gateway.ImportResult(
            statement_id, 3, 0, (), None, False, (), restored_edits=2, unmatched_edits=1
        )

    monkeypatch.setattr(gateway, "reparse", fake_reparse)
    r = client.post(f"/api/statements/{sid}/reparse", json={"issuer_id": 1, "parser_id": "llm"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    job: dict[str, object] = {}
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "done":
            break
        time.sleep(0.05)
    assert job["status"] == "done"
    assert job["restored_edits"] == 2 and job["unmatched_edits"] == 1


def test_concurrent_page_renders_do_not_crash(tmp_path: Path) -> None:
    """pdfium is not thread-safe; the Import screen fires every thumbnail request at once and
    unserialised renders segfaulted the server (the e2e job's flaky 'Import is disabled')."""
    import threading

    from personal_finance_analyzer.api.services import statement_preview

    pdf = tmp_path / "c.pdf"
    render_lines_pdf(pdf, [["Page one " * 4], ["Page two " * 4], ["Page three " * 4]])
    errors: list[BaseException] = []

    def render(page: int) -> None:
        try:
            for _ in range(15):
                assert statement_preview.render_page_png(pdf, page, 160).startswith(b"\x89PNG")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=render, args=(n,)) for n in (1, 2, 3, 1, 2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not errors
    assert not any(t.is_alive() for t in threads)
