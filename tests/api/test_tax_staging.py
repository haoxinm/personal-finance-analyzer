"""Re-upload to read: a document whose PDF was not kept can still use the page tools after the
user selects the same file again. The copy is staged temporarily and never persists (FakeLLM)."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from personal_finance_analyzer.api.app import create_app
from personal_finance_analyzer.config import IngestConfig, PrivacyConfig, Settings, save_settings
from personal_finance_analyzer.core.paths import statements_dir, tax_staging_dir
from personal_finance_analyzer.db.models import User
from personal_finance_analyzer.ingest.tax import staging
from tests.fixtures.gen.base import render_lines_pdf
from tests.llm_fakes import FakeLLM, install, remote_cfg

_NAME = "Jordan Q. Testperson"
_LINES = [
    "Form 1099-INT  Year-End Statement  Tax Year 2024",
    "Granite Fund Trust  summary of income paid to you during the year",
    "Payer identification  99-0000042      Account  SYN-000123456789",
    f"Recipient  {_NAME}    SSN 912-34-5678",
    "Interest paid to you                      1,234.56",
    "Please keep this statement with your tax records for the year.",
    "Questions about this statement may be sent to the customer service center.",
]


def _settings(*, store: bool = False) -> None:
    save_settings(
        Settings(
            llm=remote_cfg(),
            ingest=IngestConfig(tax_llm_fallback=False),
            privacy=PrivacyConfig(store_tax_pdf_copies=store, pii_terms=(_NAME,)),
        )
    )


def _reply() -> str:
    return json.dumps(
        {
            "forms": [
                {
                    "form_type": "1099-int",
                    "payer_name": "Granite Fund Trust",
                    "pages": [1],
                    "items": [{"box": "1", "amount": "1234.56"}],
                }
            ]
        }
    )


@pytest.fixture
def uid(session: Session) -> int:
    user = User(name="alice")
    session.add(user)
    session.commit()
    return user.id


@pytest.fixture
def pdf_bytes(tmp_path: Path) -> bytes:
    p = tmp_path / "ye.pdf"
    render_lines_pdf(p, [_LINES])
    return p.read_bytes()


@pytest.fixture
def doc(client: TestClient, uid: int, pdf_bytes: bytes) -> dict[str, Any]:
    """An imported document whose PDF was NOT kept (the privacy default)."""
    _settings(store=False)
    r = client.post(
        "/api/tax/documents",
        data={"user_id": str(uid)},
        files={"file": ("ye.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 201, r.text
    d: dict[str, Any] = r.json()["document"]
    assert d["has_pdf"] is False and d["staged_until"] is None
    return d


def _source(client: TestClient, doc: dict[str, Any], data: bytes, **params: Any) -> Any:
    return client.post(
        f"/api/tax/documents/{doc['id']}/source",
        params=params,
        files={"file": ("again.pdf", data, "application/pdf")},
    )


def _staged_files() -> list[Path]:
    d = tax_staging_dir()
    return sorted(d.iterdir()) if d.is_dir() else []


def test_page_tools_need_the_file_until_it_is_provided(
    client: TestClient, doc: dict[str, Any], pdf_bytes: bytes
) -> None:
    base = f"/api/tax/documents/{doc['id']}"
    for url in ("pages", "llm-preview", "pages/1/image"):
        r = client.get(f"{base}/{url}")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "pdf_needed", url
    assert client.post(f"{base}/llm-fallback", json={"pages": [1]}).status_code == 409

    r = _source(client, doc, pdf_bytes, user_id=doc["user_id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["page_number"] for p in body["pages"]] == [1] and body["expires_at"]
    assert client.get(f"{base}/pages").json() == body["pages"]
    assert client.get(f"{base}/pages/1/image").content.startswith(b"\x89PNG")
    assert client.get(f"{base}/llm-preview", params={"pages": "1"}).status_code == 200
    shown = client.get(f"/api/tax/documents/{doc['id']}").json()
    assert shown["has_pdf"] is False and shown["staged_until"] == body["expires_at"]


def test_a_different_file_is_rejected(
    client: TestClient, doc: dict[str, Any], tmp_path: Path
) -> None:
    other = tmp_path / "other.pdf"
    render_lines_pdf(other, [["something else entirely"]])
    r = _source(client, doc, other.read_bytes())
    assert r.status_code == 422
    assert r.json()["detail"] == f"This isn't the same file as {doc['original_name']}"
    assert _staged_files() == []
    assert client.get(f"/api/tax/documents/{doc['id']}/pages").status_code == 409


def test_bad_uploads_are_rejected(client: TestClient, doc: dict[str, Any]) -> None:
    assert _source(client, doc, b"not a pdf").status_code == 400
    assert _staged_files() == []


def test_ownership_is_a_404(
    client: TestClient, doc: dict[str, Any], pdf_bytes: bytes, session: Session
) -> None:
    other = User(name="bob")
    session.add(other)
    session.commit()
    assert _source(client, doc, pdf_bytes, user_id=other.id).status_code == 404
    assert _staged_files() == []
    assert _source(client, {"id": 9999}, pdf_bytes).status_code == 404


def test_staged_location_is_private_and_not_the_statements_dir(
    client: TestClient, doc: dict[str, Any], pdf_bytes: bytes
) -> None:
    assert _source(client, doc, pdf_bytes).status_code == 200
    (staged,) = _staged_files()
    assert staged.read_bytes() == pdf_bytes
    if os.name == "posix":
        assert stat.S_IMODE(tax_staging_dir().stat().st_mode) == 0o700
        assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert list(statements_dir().glob("*.pdf")) == []


def test_preview_equals_what_is_sent_and_the_copy_is_removed_after(
    client: TestClient, doc: dict[str, Any], pdf_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM(default=_reply()))
    _source(client, doc, pdf_bytes)
    base = f"/api/tax/documents/{doc['id']}"
    preview = client.get(f"{base}/llm-preview", params={"pages": "1"}).json()
    assert preview["pages"] == [1] and fake.requests == []
    r = client.post(f"{base}/llm-fallback", json={"pages": [1]})
    assert r.status_code == 200, r.text
    assert r.json()["inserted_forms"] == 1
    assert r.json()["document"]["staged_until"] is None
    assert fake.last_user_text.strip() == "\n".join(preview["requests"]).strip()
    for secret in ("912-34-5678", "99-0000042", "Testperson"):
        assert secret not in fake.last_user_text
    # Nothing persisted after the flow (store_tax_pdf_copies is off).
    assert _staged_files() == []
    assert list(statements_dir().glob("*.pdf")) == []
    assert client.get(f"{base}/pages").status_code == 409


def test_a_failed_send_keeps_the_copy_for_a_retry(
    client: TestClient, doc: dict[str, Any], pdf_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, FakeLLM(default=_reply()))
    _source(client, doc, pdf_bytes)
    r = client.post(f"/api/tax/documents/{doc['id']}/llm-fallback", json={"pages": [99]})
    assert r.status_code == 422
    assert len(_staged_files()) == 1


def test_deleting_the_document_removes_the_staged_copy(
    client: TestClient, doc: dict[str, Any], pdf_bytes: bytes
) -> None:
    _source(client, doc, pdf_bytes)
    assert len(_staged_files()) == 1
    assert client.delete(f"/api/tax/documents/{doc['id']}").status_code == 204
    assert _staged_files() == []


def test_expired_copies_are_gone_on_access_and_when_staging(
    client: TestClient, doc: dict[str, Any], pdf_bytes: bytes
) -> None:
    _source(client, doc, pdf_bytes)
    (staged,) = _staged_files()
    old = time.time() - staging.STAGING_TTL_SECONDS - 5
    os.utime(staged, (old, old))
    r = client.get(f"/api/tax/documents/{doc['id']}/pages")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "pdf_needed"
    assert _staged_files() == []
    # A stale file of another document is purged when something is staged.
    stale = tax_staging_dir() / "424242.pdf"
    stale.write_bytes(b"%PDF-")
    os.utime(stale, (old, old))
    assert _source(client, doc, pdf_bytes).status_code == 200
    assert [p.name for p in _staged_files()] == [f"{doc['id']}.pdf"]


def test_startup_purges_every_staged_copy(
    client: TestClient, doc: dict[str, Any], pdf_bytes: bytes
) -> None:
    _source(client, doc, pdf_bytes)
    assert len(_staged_files()) == 1
    create_app(token="t")
    assert _staged_files() == []


def test_kept_copy_is_used_and_staging_is_unneeded(
    client: TestClient, uid: int, pdf_bytes: bytes
) -> None:
    _settings(store=True)
    r = client.post(
        "/api/tax/documents",
        data={"user_id": str(uid)},
        files={"file": ("ye.pdf", pdf_bytes, "application/pdf")},
    )
    d = r.json()["document"]
    assert d["has_pdf"] is True
    assert client.get(f"/api/tax/documents/{d['id']}/pages").status_code == 200
    assert _staged_files() == []


def test_a_manual_document_has_no_file(client: TestClient, uid: int, pdf_bytes: bytes) -> None:
    r = client.post("/api/tax/documents/manual", json={"user_id": uid, "tax_year": 2024})
    if r.status_code != 201:
        pytest.skip("manual documents are created elsewhere")
    assert _source(client, r.json(), pdf_bytes).status_code == 409
