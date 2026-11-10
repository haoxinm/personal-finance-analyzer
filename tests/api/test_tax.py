"""`/api/tax/*`: upload hardening, import via the real pipeline on the synthetic tax fixtures,
summaries, user isolation, delete cascade, and the SSN / `stored_path` tripwire."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.core.paths import statements_dir
from personal_finance_analyzer.core.tax_types import (
    INCOME_CATEGORY_LABELS,
    TAX_CATEGORIES,
    TAX_CATEGORY_LABELS,
    FormType,
    IncomeCategory,
)
from personal_finance_analyzer.db.models import IncomeItem, TaxDocument, TaxForm, TaxItem, User
from tests.ingest.tax_support import generated_pdf

_SSN_SHAPE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EIN_SHAPE = re.compile(r"\b\d{2}-\d{7}\b")
_NINE_DIGITS = re.compile(r"(?<!\d)\d{9}(?!\d)")
#: The synthetic identifiers printed on the generated W-2 fixture.
_FIXTURE_SSN = "912-34-5678"


@pytest.fixture
def user_id(session: Session) -> int:
    user = User(name="alice")
    session.add(user)
    session.commit()
    return user.id


def _upload(
    client: TestClient,
    layout: str,
    variant: str,
    user_id: int,
    *,
    tax_year: int | None = None,
    filename: str | None = None,
) -> Any:
    path = generated_pdf(layout, variant)
    data: dict[str, str] = {"user_id": str(user_id)}
    if tax_year is not None:
        data["tax_year"] = str(tax_year)
    return client.post(
        "/api/tax/documents",
        data=data,
        files={"file": (filename or path.name, path.read_bytes(), "application/pdf")},
    )


def _all_routes(client: TestClient, document_id: int) -> dict[str, str]:
    """The raw JSON text of every tax GET route (for the tripwire)."""
    urls = [
        "/api/tax/documents",
        f"/api/tax/documents/{document_id}",
        "/api/tax/years",
        "/api/tax/summary",
        "/api/tax/summary?per_user=true",
        "/api/tax/income-by-payer",
    ]
    out = {}
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, (url, response.text)
        out[url] = response.text
    return out


# --- schema enums stay in sync with core/tax_types ---------------------------------------------


def test_schema_enums_mirror_core_vocabulary() -> None:
    assert [m.value for m in schemas.TaxFormType] == list(get_args(FormType))
    assert [m.value for m in schemas.IncomeCategoryEnum] == list(get_args(IncomeCategory))
    assert [m.value for m in schemas.TaxCategoryEnum] == list(TAX_CATEGORIES)


# --- upload hardening --------------------------------------------------------------------------


def test_upload_rejects_non_pdf(client: TestClient, user_id: int) -> None:
    response = client.post(
        "/api/tax/documents",
        data={"user_id": str(user_id)},
        files={"file": ("w2.pdf", b"not a pdf", "application/pdf")},
    )
    assert response.status_code == 400


def test_upload_rejects_wrong_content_type(client: TestClient, user_id: int) -> None:
    response = client.post(
        "/api/tax/documents",
        data={"user_id": str(user_id)},
        files={"file": ("w2.pdf", b"%PDF-1.4 x", "text/html")},
    )
    assert response.status_code == 400


def test_upload_rejects_oversized_file(client: TestClient, user_id: int) -> None:
    oversized = b"%PDF-1.4" + b"0" * (26 * 1024 * 1024)
    response = client.post(
        "/api/tax/documents",
        data={"user_id": str(user_id)},
        files={"file": ("w2.pdf", oversized, "application/pdf")},
    )
    assert response.status_code == 400


def test_upload_unknown_user_is_404(client: TestClient) -> None:
    response = _upload(client, "tax_w2", "normal", 9999)
    assert response.status_code == 404


@pytest.mark.parametrize("year", [1800, 3000])
def test_upload_invalid_tax_year_is_400(client: TestClient, user_id: int, year: int) -> None:
    assert _upload(client, "tax_w2", "normal", user_id, tax_year=year).status_code == 400


def test_upload_sanitizes_filename_and_keeps_no_pdf_copy(client: TestClient, user_id: int) -> None:
    response = _upload(client, "tax_w2", "normal", user_id, filename="../../etc/p<a>ss wd.pdf")
    assert response.status_code == 201, response.text
    assert response.json()["document"]["original_name"] == "p_a_ss wd.pdf"
    # [privacy] store_tax_pdf_copies defaults to False: the staged upload is removed.
    assert list(statements_dir().glob("*.pdf")) == []


# --- import ------------------------------------------------------------------------------------

_IMPORT_CASES = [
    ("tax_w2", "normal", ["w2"]),
    ("tax_w2", "two_employers", ["w2", "w2"]),
    ("tax_1099_int", "normal", ["1099-int"]),
    ("tax_1099_div", "normal", ["1099-div"]),
    ("tax_1099_b", "normal_summary", ["1099-b"]),
    ("tax_1099_nec_misc", "nec_normal", ["1099-nec"]),
    ("tax_1040", "with_investment_income", ["1040"]),
]


@pytest.mark.parametrize(("layout", "variant", "forms"), _IMPORT_CASES)
def test_upload_each_fixture_type(
    client: TestClient, user_id: int, layout: str, variant: str, forms: list[str]
) -> None:
    response = _upload(client, layout, variant, user_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "parsed"
    assert body["error"] is None
    assert body["inserted_forms"] == len(forms)
    assert body["document"]["form_types"] == forms
    assert body["document"]["status"] == "parsed"
    assert body["document"]["user_id"] == user_id
    assert body["income_item_count"] > 0

    detail = client.get(f"/api/tax/documents/{body['document']['id']}").json()
    assert [f["form_type"] for f in detail["forms"]] == forms
    assert sum(len(f["income_items"]) for f in detail["forms"]) == body["income_item_count"]
    assert sum(len(f["tax_items"]) for f in detail["forms"]) == body["tax_item_count"]


def test_w2_detail_contents(client: TestClient, user_id: int) -> None:
    body = _upload(client, "tax_w2", "normal", user_id).json()
    detail = client.get(f"/api/tax/documents/{body['document']['id']}").json()
    (form,) = detail["forms"]
    assert form["role"] == "source"
    assert form["payer_name"] == "Pinecrest Logistics LLC"
    assert form["payer_tin_last4"] == "0001"
    wages = [i for i in form["income_items"] if i["category"] == "salary_wages"]
    assert [w["amount_minor"] for w in wages] == [8543210]
    fed = [t for t in form["tax_items"] if t["category"] == "federal_income_tax_withheld"]
    assert [t["amount_minor"] for t in fed] == [1187640]


def test_duplicate_upload_is_a_noop(client: TestClient, user_id: int) -> None:
    first = _upload(client, "tax_w2", "normal", user_id).json()
    second = _upload(client, "tax_w2", "normal", user_id)
    assert second.status_code == 201
    body = second.json()
    assert body["status"] == "duplicate"
    assert body["document"]["id"] == first["document"]["id"]
    assert body["inserted_forms"] == 0
    assert len(client.get("/api/tax/documents").json()) == 1


def test_skipped_duplicate_names_the_earlier_file(client: TestClient, user_id: int) -> None:
    first = _upload(client, "tax_w2", "normal", user_id, filename="w2_first.pdf").json()
    path = generated_pdf("tax_w2", "normal")
    second = client.post(
        "/api/tax/documents",
        data={"user_id": str(user_id)},
        files={"file": ("w2_again.pdf", path.read_bytes() + b"\n% again\n", "application/pdf")},
    ).json()
    assert second["skipped_duplicate_forms"] == 1
    assert second["skipped_from"] == [
        {"document_id": first["document"]["id"], "original_name": "w2_first.pdf", "form_type": "w2"}
    ]
    assert any('"w2_first.pdf"' in w for w in second["warnings"])
    assert first["skipped_from"] == []


def test_non_tax_pdf_is_unsupported_with_generic_message(client: TestClient, user_id: int) -> None:
    response = _upload(client, "layout_d_bank", "normal", user_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "unsupported"
    assert body["document"]["status"] == "unsupported"
    assert body["document"]["form_types"] == []
    assert body["error"]
    assert not _SSN_SHAPE.search(response.text)
    assert client.get(f"/api/tax/documents/{body['document']['id']}").json()["forms"] == []


def test_tax_year_override_for_yearless_form(client: TestClient, user_id: int) -> None:
    first = _upload(client, "tax_w2", "no_year", user_id)
    assert first.json()["status"] == "error"
    retry = _upload(client, "tax_w2", "no_year", user_id, tax_year=2023)
    body = retry.json()
    assert body["status"] == "parsed", body
    assert body["document"]["tax_year"] == 2023
    assert body["document"]["id"] == first.json()["document"]["id"]


# --- listing / detail / delete -----------------------------------------------------------------


def test_list_filters(client: TestClient, session: Session, user_id: int) -> None:
    other = User(name="bob")
    session.add(other)
    session.commit()
    _upload(client, "tax_w2", "normal", user_id)
    _upload(client, "tax_1099_int", "normal", other.id)

    assert len(client.get("/api/tax/documents").json()) == 2
    mine = client.get("/api/tax/documents", params={"user_id": user_id}).json()
    assert [d["form_types"] for d in mine] == [["w2"]]
    assert client.get("/api/tax/documents", params={"tax_year": 2024}).json() != []
    assert client.get("/api/tax/documents", params={"tax_year": 1999}).json() == []
    assert client.get("/api/tax/documents", params={"tax_year": 1500}).status_code == 400


def test_get_missing_document_is_404(client: TestClient) -> None:
    assert client.get("/api/tax/documents/12345").status_code == 404
    assert client.delete("/api/tax/documents/12345").status_code == 404


def test_delete_cascades(client: TestClient, session: Session, user_id: int) -> None:
    doc_id = _upload(client, "tax_w2", "two_employers", user_id).json()["document"]["id"]
    assert client.delete(f"/api/tax/documents/{doc_id}").status_code == 204
    assert client.get(f"/api/tax/documents/{doc_id}").status_code == 404
    session.expire_all()
    for model in (TaxDocument, TaxForm, IncomeItem, TaxItem):
        assert session.scalar(select(func.count()).select_from(model)) == 0
    assert client.get("/api/tax/years").json() == []
    assert client.get("/api/tax/summary").json() == []


# --- years / summary / income by payer ---------------------------------------------------------


def test_years(client: TestClient, session: Session, user_id: int) -> None:
    assert client.get("/api/tax/years").json() == []
    _upload(client, "tax_w2", "normal", user_id)
    _upload(client, "tax_w2", "no_year", user_id, tax_year=2023)
    assert client.get("/api/tax/years").json() == [2024, 2023]
    assert client.get("/api/tax/years", params={"user_ids": [9999]}).json() == []


def test_summary_end_to_end(client: TestClient, user_id: int) -> None:
    _upload(client, "tax_w2", "two_employers", user_id)
    (summary,) = client.get("/api/tax/summary").json()
    assert summary["tax_year"] == 2024
    assert summary["income_basis"] == "source_documents"
    assert summary["total_income_minor"] == 8543210 + 2431050
    income = {r["category"]: r for r in summary["income_by_category"]}
    assert list(income) == [m.value for m in schemas.IncomeCategoryEnum]  # stable order
    assert income["salary_wages"]["amount_minor"] == 8543210 + 2431050
    assert income["salary_wages"]["label"] == INCOME_CATEGORY_LABELS["salary_wages"]
    assert income["qualified_dividends"]["is_memo"] is True
    assert income["salary_wages"]["is_memo"] is False
    withheld = {r["category"]: r for r in summary["withheld"]}
    assert withheld["federal_income_tax_withheld"]["amount_minor"] == 1187640 + 210400
    assert (
        withheld["federal_income_tax_withheld"]["label"]
        == TAX_CATEGORY_LABELS["federal_income_tax_withheld"]
    )
    assert [r["category"] for r in summary["fica"]] == ["social_security_tax", "medicare_tax"]
    assert summary["liability"] == []
    assert summary["refund_minor"] is None
    assert summary["document_count"] == 1 and summary["form_count"] == 2
    assert summary["withholding_rate"] is not None


def test_summary_with_return_has_liability_and_refund(client: TestClient, user_id: int) -> None:
    _upload(client, "tax_1040", "normal_wages_only", user_id)
    (summary,) = client.get("/api/tax/summary").json()
    assert summary["income_basis"] == "return"
    liability = {r["category"]: r["amount_minor"] for r in summary["liability"]}
    assert liability["total_federal_tax"] == 1054700
    assert summary["refund_minor"] == 81200
    assert summary["federal_effective_rate"] is not None
    assert summary["warnings"]


def test_summary_memo_lines(client: TestClient, user_id: int) -> None:
    _upload(client, "tax_1099_div", "normal", user_id)
    (summary,) = client.get("/api/tax/summary").json()
    memo = {m["category"]: m for m in summary["memo_by_category"]}
    assert "qualified_dividends" in memo
    assert memo["qualified_dividends"]["label"] == "Qualified dividends (memo)"


def test_summary_user_isolation_and_per_user(
    client: TestClient, session: Session, user_id: int
) -> None:
    other = User(name="bob")
    session.add(other)
    session.commit()
    _upload(client, "tax_w2", "normal", user_id)
    _upload(client, "tax_1099_int", "normal", other.id)

    mine = client.get("/api/tax/summary", params={"user_ids": [user_id]}).json()
    assert [(s["user_id"], s["total_income_minor"]) for s in mine] == [(user_id, 8543210)]
    theirs = client.get("/api/tax/summary", params={"user_ids": [other.id]}).json()
    assert theirs[0]["total_income_minor"] == 123456 + 24580

    per_user = client.get("/api/tax/summary", params={"per_user": True}).json()
    assert {s["user_id"] for s in per_user} == {user_id, other.id}
    combined = client.get("/api/tax/summary").json()
    assert combined[0]["total_income_minor"] == 8543210 + 123456 + 24580


def test_summary_validation_errors(client: TestClient) -> None:
    assert client.get("/api/tax/summary", params={"currency": "EUR"}).status_code == 400
    assert client.get("/api/tax/summary", params={"tax_years": [1500]}).status_code == 400
    assert client.get("/api/tax/income-by-payer", params={"currency": "EUR"}).status_code == 400


def test_income_by_payer(client: TestClient, user_id: int) -> None:
    _upload(client, "tax_w2", "two_employers", user_id)
    rows = client.get("/api/tax/income-by-payer", params={"tax_years": [2024]}).json()
    assert [(r["payer_name"], r["form_type"], r["category"], r["amount_minor"]) for r in rows] == [
        ("Pinecrest Logistics LLC", "w2", "salary_wages", 8543210),
        ("Alder Ridge Brewing Co", "w2", "salary_wages", 2431050),
    ]
    assert rows[0]["label"] == INCOME_CATEGORY_LABELS["salary_wages"]
    assert client.get("/api/tax/income-by-payer", params={"tax_years": [2020]}).json() == []


# --- privacy tripwire --------------------------------------------------------------------------


def test_no_ssn_or_stored_path_anywhere(client: TestClient, user_id: int) -> None:
    from personal_finance_analyzer.ingest.extract import extract

    assert _FIXTURE_SSN in extract(generated_pdf("tax_w2", "normal"), write_cache=False).full_text

    texts: list[str] = []
    last_doc = 0
    for layout, variant, _ in _IMPORT_CASES:
        response = _upload(client, layout, variant, user_id)
        texts.append(response.text)
        last_doc = response.json()["document"]["id"]
    texts.append(_upload(client, "layout_d_bank", "normal", user_id).text)
    texts.extend(_all_routes(client, last_doc).values())
    for doc in client.get("/api/tax/documents").json():
        texts.append(client.get(f"/api/tax/documents/{doc['id']}").text)

    for text in texts:
        assert "stored_path" not in text
        assert not _SSN_SHAPE.search(text), text[:200]
        assert not _EIN_SHAPE.search(text), text[:200]
        assert not _NINE_DIGITS.search(text), text[:200]
        assert "912345678" not in text.replace("-", "")


def test_openapi_schemas_never_expose_stored_path_or_full_tin(app_instance: Any) -> None:
    components = app_instance.openapi()["components"]["schemas"]
    for name in ("TaxDocument", "TaxDocumentDetail", "TaxFormDetail"):
        props = set(components[name]["properties"])
        assert "stored_path" not in props
        assert not {"ssn", "tin", "payer_tin", "file_sha256"} & props


def test_transaction_export_unaffected_by_tax_data(client: TestClient, user_id: int) -> None:
    _upload(client, "tax_w2", "normal", user_id)
    response = client.get("/api/export", params={"format": "csv"})
    assert response.status_code == 200
    assert "Pinecrest" not in response.text


def test_fixtures_exist() -> None:
    assert Path(generated_pdf("tax_w2", "normal")).is_file()


def test_upload_scrubs_ssn_shape_in_original_name(client: TestClient, user_id: int) -> None:
    response = _upload(client, "tax_w2", "normal", user_id, filename="W2 123-45-6789.pdf")
    assert response.status_code == 201, response.text
    name = response.json()["document"]["original_name"]
    assert "123-45" not in name and not re.search(r"\d{6,}", name)
    listing = client.get("/api/tax/documents").text
    assert "123-45" not in listing


def test_same_pdf_for_two_users_is_not_a_duplicate(client: TestClient, session: Session) -> None:
    a, b = User(name="alice"), User(name="bob")
    session.add_all([a, b])
    session.commit()
    first = _upload(client, "tax_w2", "normal", a.id).json()
    second = _upload(client, "tax_w2", "normal", b.id).json()
    assert (first["status"], second["status"]) == ("parsed", "parsed")
    assert second["document"]["user_id"] == b.id
    assert first["document"]["id"] != second["document"]["id"]


def test_upload_uses_the_llm_fallback_only_when_opted_in(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import json

    from personal_finance_analyzer.config import IngestConfig, Settings, save_settings
    from tests.fixtures.gen.base import render_lines_pdf
    from tests.llm_fakes import FakeLLM, install, remote_cfg

    lines = [
        "Form 1099-INT  Year-End Statement  Tax Year 2024",
        "Granite Fund Trust  summary of income paid to you during the year",
        "Interest paid to you                      1,234.56",
        "Please keep this statement with your tax records for the year.",
        "Questions about this statement may be sent to the customer service center.",
    ]
    reply = json.dumps(
        {
            "forms": [
                {
                    "form_type": "1099-int",
                    "pages": [1],
                    "items": [{"box": "1", "amount": "1234.56"}],
                }
            ]
        }
    )
    fake = install(monkeypatch, FakeLLM(default=reply))
    pdf = tmp_path / "ye.pdf"
    render_lines_pdf(pdf, [lines])

    def post(name: str) -> Any:
        return client.post(
            "/api/tax/documents",
            data={"user_id": str(user_id)},
            files={"file": (name, pdf.read_bytes() + name.encode(), "application/pdf")},
        )

    save_settings(Settings(llm=remote_cfg(), ingest=IngestConfig(tax_llm_fallback=False)))
    off = post("off.pdf")
    assert off.json()["status"] == "unsupported" and off.json()["llm_used"] is False
    assert fake.requests == []

    save_settings(Settings(llm=remote_cfg(), ingest=IngestConfig(tax_llm_fallback=True)))
    on = post("on.pdf")
    body = on.json()
    assert body["status"] == "parsed", body
    assert (body["llm_used"], body["llm_form_count"]) == (True, 1)
    detail = client.get(f"/api/tax/documents/{body['document']['id']}").json()
    (form,) = detail["forms"]
    assert form["extraction_method"] == "llm" and form["needs_review"] is True
