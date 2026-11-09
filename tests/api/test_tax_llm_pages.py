"""Tax document page tools: thumbnails, the network-free LLM preview and the on-demand read of
chosen pages (FakeLLM only; no socket)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from personal_finance_analyzer.config import IngestConfig, PrivacyConfig, Settings, save_settings
from personal_finance_analyzer.db.models import User
from tests.fixtures.gen.base import render_lines_pdf
from tests.ingest.tax_support import generated_pdf
from tests.llm_fakes import FakeLLM, install, remote_cfg

_NAME = "Jordan Q. Testperson"
_UNREADABLE = [
    "Form 1099-INT  Year-End Statement  Tax Year 2024",
    "Granite Fund Trust  summary of income paid to you during the year",
    "Payer identification  99-0000042      Account  SYN-000123456789",
    f"Recipient  {_NAME}    SSN 912-34-5678",
    "Mailing address  100 Example Lane Testville ZZ 00000",
    "Interest paid to you                      1,234.56",
    "Federal income tax withheld                  12.00",
    "Please keep this statement with your tax records for the year.",
    "Questions about this statement may be sent to the customer service center.",
]
_COVER = [
    "Supplemental schedule",
    f"Prepared for {_NAME}  phone 555-010-4477",
    "Adjustment                                   25.00",
]
_SECRETS = (
    "912-34-5678",
    "99-0000042",
    "000123456789",
    "Testperson",
    "Example Lane",
    "555-010-4477",
)


def _settings(*, store: bool = True, llm: bool = True, auto: bool = False) -> None:
    save_settings(
        Settings(
            llm=remote_cfg() if llm else remote_cfg(enabled=False),
            ingest=IngestConfig(tax_llm_fallback=auto),
            privacy=PrivacyConfig(store_tax_pdf_copies=store, pii_terms=(_NAME, "555-010-4477")),
        )
    )


@pytest.fixture
def user_id(session: Session) -> int:
    user = User(name="alice")
    session.add(user)
    session.commit()
    return user.id


def _post(client: TestClient, uid: int, path: Path, name: str = "ye.pdf") -> dict[str, Any]:
    r = client.post(
        "/api/tax/documents",
        data={"user_id": str(uid)},
        files={"file": (name, path.read_bytes(), "application/pdf")},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


@pytest.fixture
def doc(client: TestClient, user_id: int, tmp_path: Path) -> Iterator[dict[str, Any]]:
    """An imported (unsupported: nothing the parsers can read) two-page file with a kept PDF."""
    _settings()
    pdf = tmp_path / "ye.pdf"
    render_lines_pdf(pdf, [_COVER, _UNREADABLE])
    body = _post(client, user_id, pdf)
    assert body["status"] == "unsupported" and body["document"]["has_pdf"] is True
    yield body["document"]


def _reply() -> str:
    return json.dumps(
        {
            "forms": [
                {
                    "form_type": "1099-int",
                    "payer_name": "Granite Fund Trust",
                    "payer_tin_last4": "0042",
                    "account_last4": "6789",
                    "pages": [1],
                    "items": [{"box": "1", "amount": "1234.56"}, {"box": "4", "amount": "12.00"}],
                }
            ]
        }
    )


def test_preview_defaults_to_the_fallback_pages_and_is_redacted(
    client: TestClient, doc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM())
    r = client.get(f"/api/tax/documents/{doc['id']}/llm-preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "remote" and body["model"] == "test-model"
    assert body["pages"] == [2] and len(body["requests"]) == 1
    assert body["total_chars"] == len(body["requests"][0])
    wire = "\n".join(body["requests"])
    assert "1,234.56" in wire and "=== PAGE 2 ===" in wire
    for secret in _SECRETS:
        assert secret not in wire
    assert not re.search(r"\b\d{3}-\d{2}-\d{4}\b|\b\d{2}-\d{7}\b", wire)
    assert fake.requests == []  # a preview never sends


def test_preview_works_with_the_automatic_fallback_off(
    client: TestClient, doc: dict[str, Any]
) -> None:
    _settings(auto=False)
    assert client.get(f"/api/tax/documents/{doc['id']}/llm-preview").json()["pages"] == [2]


def test_preview_explicit_pages_override_the_default(
    client: TestClient, doc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM())
    r = client.get(f"/api/tax/documents/{doc['id']}/llm-preview", params={"pages": "1"})
    body = r.json()
    assert body["pages"] == [1]
    assert "Adjustment" in body["requests"][0] and "Granite" not in body["requests"][0]
    assert "Testperson" not in body["requests"][0] and "555-010-4477" not in body["requests"][0]
    assert body["redactions"] >= 2  # the name and the phone entry
    both = client.get(f"/api/tax/documents/{doc['id']}/llm-preview", params={"pages": "1-2"}).json()
    assert both["pages"] == [1, 2]
    assert fake.requests == []


def test_preview_bad_pages_is_422(client: TestClient, doc: dict[str, Any]) -> None:
    r = client.get(f"/api/tax/documents/{doc['id']}/llm-preview", params={"pages": "x"})
    assert r.status_code == 422


def test_preview_without_a_configured_llm_is_mode_none(
    client: TestClient, doc: dict[str, Any]
) -> None:
    _settings(llm=False)
    body = client.get(f"/api/tax/documents/{doc['id']}/llm-preview").json()
    assert (body["mode"], body["pages"], body["requests"]) == ("none", [], [])


def test_form_1040_pages_are_never_sent(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings()
    fake = install(monkeypatch, FakeLLM())
    d = _post(client, user_id, generated_pdf("tax_1040", "with_investment_income"))["document"]
    pages = client.get(f"/api/tax/documents/{d['id']}/pages").json()
    assert pages and all(p["excluded_reason"] for p in pages)
    r = client.get(
        f"/api/tax/documents/{d['id']}/llm-preview",
        params={"pages": ",".join(str(p["page_number"]) for p in pages)},
    )
    assert r.json()["pages"] == [] and r.json()["requests"] == []
    send = client.post(f"/api/tax/documents/{d['id']}/llm-fallback", json={"pages": [1]})
    assert send.status_code == 422
    assert fake.requests == []


def test_pages_list_marks_the_suggested_pages(client: TestClient, doc: dict[str, Any]) -> None:
    rows = client.get(f"/api/tax/documents/{doc['id']}/pages").json()
    assert [(r["page_number"], r["suggested"], r["excluded_reason"]) for r in rows] == [
        (1, False, None),
        (2, True, None),
    ]


def test_page_image_is_a_png(client: TestClient, doc: dict[str, Any]) -> None:
    r = client.get(f"/api/tax/documents/{doc['id']}/pages/1/image", params={"width": 200})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")
    assert client.get(f"/api/tax/documents/{doc['id']}/pages/9/image").status_code == 404


def test_ownership_is_a_404(client: TestClient, doc: dict[str, Any], session: Session) -> None:
    other = User(name="bob")
    session.add(other)
    session.commit()
    base = f"/api/tax/documents/{doc['id']}"
    for url in (f"{base}/llm-preview", f"{base}/pages", f"{base}/pages/1/image"):
        assert client.get(url, params={"user_id": other.id}).status_code == 404, url
        assert client.get(url, params={"user_id": doc["user_id"]}).status_code == 200, url
    r = client.post(f"{base}/llm-fallback", params={"user_id": other.id}, json={"pages": [1]})
    assert r.status_code == 404


def test_no_kept_pdf_means_no_page_tools(client: TestClient, user_id: int, tmp_path: Path) -> None:
    _settings(store=False)
    pdf = tmp_path / "ye.pdf"
    render_lines_pdf(pdf, [_UNREADABLE])
    d = _post(client, user_id, pdf)["document"]
    assert d["has_pdf"] is False
    for url in ("pages", "llm-preview", "pages/1/image"):
        r = client.get(f"/api/tax/documents/{d['id']}/{url}")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "pdf_needed"


def test_send_reads_the_chosen_pages_and_tags_the_forms(
    client: TestClient, doc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM(default=_reply()))
    # The automatic setting is off (default): an explicit click is the consent.
    r = client.post(f"/api/tax/documents/{doc['id']}/llm-fallback", json={"pages": [2]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["inserted_forms"], body["skipped_duplicate_forms"], body["requests_sent"]) == (
        1,
        0,
        1,
    )
    assert body["pages"] == [2] and body["income_item_count"] >= 1
    detail = body["document"]
    assert detail["status"] == "parsed" and detail["error_detail"] is None
    (form,) = detail["forms"]
    assert form["extraction_method"] == "llm" and form["needs_review"] is True
    assert any("review" in w for w in detail["warnings"])
    wire = json.dumps(fake.requests)
    assert "1,234.56" in wire
    for secret in _SECRETS:
        assert secret not in wire
    # Reading the same pages again stores nothing new.
    again = client.post(f"/api/tax/documents/{doc['id']}/llm-fallback", json={"pages": [2]}).json()
    assert (again["inserted_forms"], again["skipped_duplicate_forms"]) == (0, 1)
    assert len(again["document"]["forms"]) == 1
    # The summary sees the figures.
    summary = client.get("/api/tax/summary").json()
    assert summary and summary[0]["form_count"] == 1


def test_send_only_the_chosen_pages(
    client: TestClient, doc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM(default=json.dumps({"forms": []})))
    r = client.post(f"/api/tax/documents/{doc['id']}/llm-fallback", json={"pages": [1]})
    assert r.status_code == 200 and r.json()["inserted_forms"] == 0
    assert "Adjustment" in fake.last_user_text and "Granite" not in fake.last_user_text


def test_send_needs_a_configured_llm_and_valid_pages(
    client: TestClient, doc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM())
    url = f"/api/tax/documents/{doc['id']}/llm-fallback"
    assert client.post(url, json={"pages": [99]}).status_code == 422
    assert client.post(url, json={"pages": []}).status_code == 422
    _settings(llm=False)
    assert client.post(url, json={"pages": [1]}).status_code == 409
    assert fake.requests == []


def test_deleting_a_document_removes_its_kept_pdf(client: TestClient, doc: dict[str, Any]) -> None:
    from personal_finance_analyzer.core.paths import documents_dir

    (kept,) = documents_dir().rglob("*.pdf")
    assert kept.relative_to(documents_dir()).parts[1] == "Tax"  # a readable path, not a hash
    assert client.delete(f"/api/tax/documents/{doc['id']}").status_code == 204
    assert list(documents_dir().rglob("*.pdf")) == []
    assert not kept.parent.exists()  # the folders it left empty are gone too


def test_send_adds_a_form_to_a_partly_parsed_document(
    client: TestClient, user_id: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings()
    pdf = tmp_path / "p.pdf"
    render_lines_pdf(
        pdf,
        [
            ["Tax Year 2024", "Form 1099-INT Interest Income", "1 Interest income   25.00"],
            [
                "Tax Year 2024",
                "Form 1099-R Distributions From Pensions",
                "1 Gross distribution   1,234.00",
            ],
        ],
    )
    first = _post(client, user_id, pdf, "p.pdf")
    assert first["status"] == "parsed" and first["document"]["form_types"] == ["1099-int"]
    reply = json.dumps(
        {
            "forms": [
                {
                    "form_type": "1099-misc",
                    "payer_name": "Pension Co",
                    "pages": [2],
                    "items": [{"box": "3", "amount": "1234.00"}],
                }
            ]
        }
    )
    fake = install(monkeypatch, FakeLLM(default=reply))
    pid = first["document"]["id"]
    preview = client.get(f"/api/tax/documents/{pid}/llm-preview").json()
    assert preview["pages"] == [2] and fake.requests == []
    body = client.post(f"/api/tax/documents/{pid}/llm-fallback", json={"pages": [2]}).json()
    assert body["inserted_forms"] == 1
    methods = {f["form_type"]: f["extraction_method"] for f in body["document"]["forms"]}
    assert methods == {"1099-int": "parser", "1099-misc": "llm"}
    assert "=== PAGE 2" in fake.last_user_text and "=== PAGE 1" not in fake.last_user_text


def test_preview_huge_or_out_of_range_pages_is_422_without_expanding(
    client: TestClient, doc: dict[str, Any]
) -> None:
    url = f"/api/tax/documents/{doc['id']}/llm-preview"
    for spec in ("1-999999999999", "3", "1-3", "99999999999999"):
        r = client.get(url, params={"pages": spec})
        assert r.status_code == 422, (spec, r.text)
    assert client.get(url, params={"pages": "1-2"}).status_code == 200


def test_default_preview_never_includes_1040_pages(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings()
    install(monkeypatch, FakeLLM())
    d = _post(client, user_id, generated_pdf("tax_1040", "with_investment_income"))["document"]
    body = client.get(f"/api/tax/documents/{d['id']}/llm-preview").json()
    assert body["pages"] == [] and body["requests"] == []


def test_load_plan_without_pages_drops_excluded_default_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from personal_finance_analyzer.core.tax_types import UnparsedRegion
    from personal_finance_analyzer.ingest.tax import on_demand

    region = UnparsedRegion("unknown", (1,), "unreadable")
    monkeypatch.setattr(on_demand, "fallback_targets", lambda *a, **k: ((region,), 2024))
    plan = on_demand.load_plan(
        generated_pdf("tax_1040", "with_investment_income"), pii_terms=(), pages=None
    )
    assert plan.pages == () and plan.default_pages == () and plan.regions == ()
