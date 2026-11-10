"""`/api/tax` manual edit routes: every route, validation, provenance, summaries, isolation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.models import TaxDocument, TaxForm, User
from personal_finance_analyzer.ingest.tax.pipeline import import_tax_document
from tests.ingest.tax_support import generated_pdf

_SSN_SHAPE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


@pytest.fixture
def user_id(session: Session) -> int:
    user = User(name="alice")
    session.add(user)
    session.commit()
    return user.id


@pytest.fixture
def other_user_id(session: Session) -> int:
    user = User(name="bob")
    session.add(user)
    session.commit()
    return user.id


def _manual_doc(client: TestClient, user_id: int, year: int = 2024) -> dict[str, Any]:
    r = client.post("/api/tax/documents/manual", json={"user_id": user_id, "tax_year": year})
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _w2_body(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "form_type": "w2",
        "payer_name": "Acme Corp",
        "payer_tin_last4": "1234",
        "income_items": [{"category": "salary_wages", "amount_minor": 100000, "box": "1"}],
        "tax_items": [{"category": "federal_income_tax_withheld", "amount_minor": 10000}],
    }
    body.update(kw)
    return body


def _form(client: TestClient, doc_id: int, **kw: Any) -> dict[str, Any]:
    r = client.post(f"/api/tax/documents/{doc_id}/forms", json=_w2_body(**kw))
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _summary(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    r = client.get("/api/tax/summary", params=params)
    assert r.status_code == 200
    return r.json()  # type: ignore[no-any-return]


def test_create_manual_document(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    assert doc["original_name"] == "Manual entry"
    assert doc["status"] == "parsed" and doc["tax_year"] == 2024 and doc["form_types"] == []
    named = client.post(
        "/api/tax/documents/manual",
        json={"user_id": user_id, "tax_year": 2023, "original_name": "Paper W-2"},
    )
    assert named.status_code == 201 and named.json()["original_name"] == "Paper W-2"
    assert client.get(f"/api/tax/documents/{doc['id']}").json()["forms"] == []


def test_create_manual_document_errors(client: TestClient, user_id: int) -> None:
    assert (
        client.post(
            "/api/tax/documents/manual", json={"user_id": 999, "tax_year": 2024}
        ).status_code
        == 404
    )
    for year in (1999, 2100):
        r = client.post("/api/tax/documents/manual", json={"user_id": user_id, "tax_year": year})
        assert r.status_code == 422
    r = client.post("/api/tax/documents/manual", json={"user_id": user_id})
    assert r.status_code == 422


def test_create_form_returns_detail_and_summary_changes(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    form = _form(client, doc["id"])
    assert form["extraction_method"] == "manual"
    assert form["is_user_edited"] is False and form["needs_review"] is False
    assert form["role"] == "source" and form["tax_year"] == 2024
    assert form["income_items"][0]["origin"] == "manual"
    assert form["income_items"][0]["source_form"] == "W-2"
    assert form["tax_items"][0]["origin"] == "manual"
    detail = client.get(f"/api/tax/documents/{doc['id']}").json()
    assert detail["form_types"] == ["w2"]
    assert detail["forms"][0]["id"] == form["id"]
    (summary,) = _summary(client)
    assert summary["total_income_minor"] == 100000
    assert summary["form_count"] == 1


def test_create_form_errors(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    url = f"/api/tax/documents/{doc['id']}/forms"
    assert client.post("/api/tax/documents/9999/forms", json=_w2_body()).status_code == 404
    bad_bodies = [
        _w2_body(payer_name="123-45-6789"),
        _w2_body(payer_name="123456789"),
        _w2_body(payer_tin_last4="12345"),
        _w2_body(payer_tin_last4="123-45-6789"),
        _w2_body(account_last4="12"),
        _w2_body(form_type="1099-zzz"),
        _w2_body(filing_status="bogus"),
        _w2_body(income_items=[{"category": "salary_wages", "amount_minor": -1}]),
        _w2_body(income_items=[{"category": "nope", "amount_minor": 1}]),
        _w2_body(tax_items=[{"category": "medicare_tax", "amount_minor": -1}]),
        _w2_body(tax_items=[{"category": "nope", "amount_minor": 1}]),
        _w2_body(unknown_field=1),
        _w2_body(tax_year=1999),
    ]
    for body in bad_bodies:
        assert client.post(url, json=body).status_code == 422, body
    # Nothing was created by the rejected requests.
    assert client.get(f"/api/tax/documents/{doc['id']}").json()["forms"] == []


def test_capital_loss_allowed_negative(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    form = _form(
        client,
        doc["id"],
        form_type="1099-b",
        income_items=[{"category": "long_term_capital_gain", "amount_minor": -500}],
        tax_items=[],
    )
    assert form["income_items"][0]["amount_minor"] == -500


def test_year_mismatch_is_400(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    _form(client, doc["id"])
    r = client.post(f"/api/tax/documents/{doc['id']}/forms", json=_w2_body(tax_year=2023))
    assert r.status_code == 400


def test_patch_form_semantics(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    form = _form(client, doc["id"])
    r = client.patch(f"/api/tax/forms/{form['id']}", json={"payer_name": "Renamed"})
    assert r.status_code == 200
    body = r.json()
    assert body["payer_name"] == "Renamed" and body["is_user_edited"] is True
    assert body["payer_tin_last4"] == "1234"  # untouched
    cleared = client.patch(f"/api/tax/forms/{form['id']}", json={"payer_tin_last4": None}).json()
    assert cleared["payer_tin_last4"] is None
    assert client.patch("/api/tax/forms/9999", json={"payer_name": "x"}).status_code == 404
    for bad in (
        {"payer_name": "123-45-6789"},
        {"payer_tin_last4": "12345"},
        {"form_type": "nope"},
        {"form_type": None},
        {"tax_year": 3000},
        {"bogus": 1},
    ):
        assert client.patch(f"/api/tax/forms/{form['id']}", json=bad).status_code == 422, bad


def test_patch_form_type_changes_role_and_summary_basis(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    form = _form(client, doc["id"])
    assert _summary(client)[0]["income_basis"] == "source_documents"
    body = client.patch(f"/api/tax/forms/{form['id']}", json={"form_type": "1040"}).json()
    assert body["role"] == "return"
    assert _summary(client)[0]["income_basis"] == "return"


def test_patch_form_year_moves_items(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    form = _form(client, doc["id"])
    r = client.patch(f"/api/tax/forms/{form['id']}", json={"tax_year": 2023})
    assert r.status_code == 200 and r.json()["tax_year"] == 2023
    assert [s["tax_year"] for s in _summary(client)] == [2023]
    assert client.get(f"/api/tax/documents/{doc['id']}").json()["tax_year"] == 2023


def test_patch_form_year_moves_sibling_forms_too(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    first = _form(client, doc["id"])
    second = _form(client, doc["id"])
    r = client.patch(f"/api/tax/forms/{second['id']}", json={"tax_year": 2023})
    assert r.status_code == 200 and r.json()["tax_year"] == 2023
    detail = client.get(f"/api/tax/documents/{doc['id']}").json()
    assert detail["tax_year"] == 2023
    assert {f["tax_year"] for f in detail["forms"]} == {2023}
    assert first["id"] in {f["id"] for f in detail["forms"]}


def test_delete_form_updates_summary(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    form = _form(client, doc["id"])
    assert client.delete(f"/api/tax/forms/{form['id']}").status_code == 204
    assert client.delete(f"/api/tax/forms/{form['id']}").status_code == 404
    assert _summary(client) == []
    assert client.get(f"/api/tax/documents/{doc['id']}").json()["forms"] == []


def test_income_item_routes(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    form = _form(client, doc["id"])
    url = f"/api/tax/forms/{form['id']}/income-items"
    r = client.post(url, json={"category": "interest", "amount_minor": 500})
    assert r.status_code == 201
    item = r.json()
    assert item["origin"] == "manual" and item["category"] == "interest"
    assert _summary(client)[0]["total_income_minor"] == 100500
    patched = client.patch(f"/api/tax/income-items/{item['id']}", json={"amount_minor": 900})
    assert patched.status_code == 200 and patched.json()["amount_minor"] == 900
    assert _summary(client)[0]["total_income_minor"] == 100900
    assert client.delete(f"/api/tax/income-items/{item['id']}").status_code == 204
    assert _summary(client)[0]["total_income_minor"] == 100000
    assert client.delete(f"/api/tax/income-items/{item['id']}").status_code == 404
    assert (
        client.patch(f"/api/tax/income-items/{item['id']}", json={"label": "x"}).status_code == 404
    )
    assert (
        client.post(
            "/api/tax/forms/9999/income-items", json={"category": "other", "amount_minor": 1}
        ).status_code
        == 404
    )
    for bad in (
        {"category": "salary_wages", "amount_minor": -1},
        {"category": "nope", "amount_minor": 1},
        {"category": "other", "amount_minor": 10**13},
        {"category": "other", "amount_minor": 1.5},
        {"category": "other", "amount_minor": 1, "label": "x" * 81},
    ):
        assert client.post(url, json=bad).status_code == 422, bad


def test_income_item_patch_negative_rule(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    form = _form(client, doc["id"])
    wage = form["income_items"][0]
    assert (
        client.patch(f"/api/tax/income-items/{wage['id']}", json={"amount_minor": -5}).status_code
        == 422
    )
    r = client.patch(
        f"/api/tax/income-items/{wage['id']}",
        json={"category": "long_term_capital_gain", "amount_minor": -5},
    )
    assert r.status_code == 200 and r.json()["amount_minor"] == -5


def test_tax_item_routes(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    form = _form(client, doc["id"])
    url = f"/api/tax/forms/{form['id']}/tax-items"
    r = client.post(
        url,
        json={
            "category": "state_income_tax_withheld",
            "amount_minor": 300,
            "jurisdiction": "CA",
            "is_estimate": True,
        },
    )
    assert r.status_code == 201
    item = r.json()
    assert item["jurisdiction"] == "CA" and item["is_estimate"] is True
    assert item["origin"] == "manual"
    patched = client.patch(
        f"/api/tax/tax-items/{item['id']}", json={"jurisdiction": None, "amount_minor": 450}
    )
    assert patched.status_code == 200
    assert patched.json()["jurisdiction"] is None and patched.json()["amount_minor"] == 450
    withheld = {w["category"]: w["amount_minor"] for w in _summary(client)[0]["withheld"]}
    assert withheld["state_income_tax_withheld"] == 450
    assert client.delete(f"/api/tax/tax-items/{item['id']}").status_code == 204
    assert client.delete(f"/api/tax/tax-items/{item['id']}").status_code == 404
    assert client.patch(f"/api/tax/tax-items/{item['id']}", json={"label": "x"}).status_code == 404
    assert (
        client.post(
            "/api/tax/forms/9999/tax-items", json={"category": "refund", "amount_minor": 1}
        ).status_code
        == 404
    )
    for bad in (
        {"category": "medicare_tax", "amount_minor": -1},
        {"category": "nope", "amount_minor": 1},
    ):
        assert client.post(url, json=bad).status_code == 422, bad
    assert (
        client.patch(
            f"/api/tax/tax-items/{form['tax_items'][0]['id']}", json={"amount_minor": -1}
        ).status_code
        == 422
    )


def test_adding_form_to_error_document_flips_it(
    client: TestClient, session: Session, user_id: int
) -> None:
    doc = TaxDocument(
        user_id=user_id,
        file_sha256="h",
        original_name="scan.pdf",
        status="unsupported",
        error_detail="not a tax form",
        tax_year=2024,
    )
    session.add(doc)
    session.commit()
    assert _summary(client) == []
    _form(client, doc.id)
    detail = client.get(f"/api/tax/documents/{doc.id}").json()
    assert detail["status"] == "parsed" and detail["error_detail"] is None
    assert "Figures on this document were entered manually." in detail["warnings"]
    assert _summary(client)[0]["total_income_minor"] == 100000


def test_edit_imported_form_is_reflected_and_marked(client: TestClient, user_id: int) -> None:
    path = generated_pdf("tax_w2", "normal")
    imported = client.post(
        "/api/tax/documents",
        data={"user_id": str(user_id)},
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
    ).json()
    doc = client.get(f"/api/tax/documents/{imported['document']['id']}").json()
    form = doc["forms"][0]
    assert form["extraction_method"] == "parser" and form["is_user_edited"] is False
    assert {i["origin"] for i in form["income_items"]} == {"parser"}
    wages = next(i for i in form["income_items"] if i["category"] == "salary_wages")
    before = _summary(client)[0]["total_income_minor"]
    r = client.patch(
        f"/api/tax/income-items/{wages['id']}", json={"amount_minor": wages["amount_minor"] + 100}
    )
    assert r.status_code == 200 and r.json()["origin"] == "manual"
    assert _summary(client)[0]["total_income_minor"] == before + 100
    after = client.get(f"/api/tax/documents/{doc['id']}").json()["forms"][0]
    assert after["is_user_edited"] is True and after["extraction_method"] == "parser"


def test_confirm_form(client: TestClient, session: Session, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    form = _form(client, doc["id"])
    row = session.get(TaxForm, form["id"])
    assert row is not None
    row.extraction_method = "llm"
    for item in (*row.income_items, *row.tax_items):
        item.origin = "llm"
    session.commit()
    flagged = client.get(f"/api/tax/documents/{doc['id']}").json()["forms"][0]
    assert flagged["needs_review"] is True
    r = client.post(f"/api/tax/forms/{form['id']}/confirm")
    assert r.status_code == 200
    body = r.json()
    assert body["needs_review"] is False
    assert {i["origin"] for i in body["income_items"]} == {"manual"}
    assert client.post("/api/tax/forms/9999/confirm").status_code == 404


def test_user_isolation(client: TestClient, user_id: int, other_user_id: int) -> None:
    mine = _manual_doc(client, user_id)
    theirs = _manual_doc(client, other_user_id)
    _form(client, mine["id"])
    other_form = _form(
        client, theirs["id"], income_items=[{"category": "interest", "amount_minor": 7}]
    )
    assert _summary(client, user_ids=user_id)[0]["total_income_minor"] == 100000
    assert _summary(client, user_ids=other_user_id)[0]["total_income_minor"] == 7
    client.delete(f"/api/tax/forms/{other_form['id']}")
    assert _summary(client, user_ids=other_user_id) == []
    assert _summary(client, user_ids=user_id)[0]["total_income_minor"] == 100000
    # The form created through a document belongs to that document's user.
    detail = client.get(f"/api/tax/documents/{mine['id']}").json()
    assert detail["user_id"] == user_id


def test_duplicate_manual_forms_do_not_collide(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    _form(client, doc["id"])
    _form(client, doc["id"])
    assert _summary(client)[0]["form_count"] == 2
    assert _summary(client)[0]["total_income_minor"] == 200000


def test_responses_never_contain_identifier_shapes(client: TestClient, user_id: int) -> None:
    doc = _manual_doc(client, user_id)
    form = _form(client, doc["id"], payer_name="Acme 12-3456789")
    texts = [
        client.get(f"/api/tax/documents/{doc['id']}").text,
        client.get("/api/tax/summary").text,
        client.get("/api/tax/income-by-payer").text,
        str(form),
    ]
    for text in texts:
        assert "3456789" not in text.replace("6789", "")
        assert not _SSN_SHAPE.search(text)
    assert "stored_path" not in texts[0]
    row = client.get(f"/api/tax/documents/{doc['id']}").json()["forms"][0]
    assert row["payer_name"] is not None and "12-3456789" not in row["payer_name"]


# --- ownership check (`user_id` query) and reassigning a document ------------------------------


def _owned_ids(client: TestClient, owner: int) -> dict[str, int]:
    doc = _manual_doc(client, owner)
    form = _form(client, doc["id"])
    return {
        "doc": doc["id"],
        "form": form["id"],
        "income": form["income_items"][0]["id"],
        "tax": form["tax_items"][0]["id"],
    }


_ITEM_BODY = {"category": "interest", "amount_minor": 5}
_TAX_BODY = {"category": "state_income_tax_withheld", "amount_minor": 5}
#: (method, url template, json body); `{doc}` / `{form}` / `{income}` / `{tax}` are filled in.
_ID_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/tax/documents/{doc}", None),
    ("POST", "/api/tax/documents/{doc}/forms", _w2_body(income_items=[], tax_items=[])),
    ("PATCH", "/api/tax/documents/{doc}", {"user_id": 1}),
    ("PATCH", "/api/tax/forms/{form}", {"payer_name": "Z"}),
    ("POST", "/api/tax/forms/{form}/confirm", None),
    ("POST", "/api/tax/forms/{form}/income-items", _ITEM_BODY),
    ("PATCH", "/api/tax/income-items/{income}", {"amount_minor": 7}),
    ("POST", "/api/tax/forms/{form}/tax-items", _TAX_BODY),
    ("PATCH", "/api/tax/tax-items/{tax}", {"amount_minor": 7}),
    ("DELETE", "/api/tax/income-items/{income}", None),
    ("DELETE", "/api/tax/tax-items/{tax}", None),
    ("DELETE", "/api/tax/forms/{form}", None),
    ("DELETE", "/api/tax/documents/{doc}", None),
]


@pytest.mark.parametrize(("method", "url", "body"), _ID_ROUTES)
def test_id_routes_check_ownership(
    client: TestClient,
    user_id: int,
    other_user_id: int,
    method: str,
    url: str,
    body: dict[str, Any] | None,
) -> None:
    ids = _owned_ids(client, user_id)
    path = url.format(**ids)
    denied = client.request(method, path, params={"user_id": other_user_id}, json=body)
    assert denied.status_code == 404, denied.text
    # Nothing changed, and the wrong-user answer is the same as for a missing row.
    detail = client.get(f"/api/tax/documents/{ids['doc']}", params={"user_id": user_id}).json()
    assert detail["forms"][0]["payer_name"] == "Acme Corp"
    assert len(detail["forms"][0]["income_items"]) == 1
    missing = client.request(
        method, path.replace(str(ids["doc"]), "9999"), params={"user_id": user_id}, json=body
    )
    if "{doc}" in url:
        assert missing.status_code == 404
        assert denied.json() == missing.json()


@pytest.mark.parametrize(("method", "url", "body"), _ID_ROUTES)
def test_id_routes_succeed_with_matching_or_no_user(
    client: TestClient,
    user_id: int,
    method: str,
    url: str,
    body: dict[str, Any] | None,
) -> None:
    if body == {"user_id": 1}:
        body = {"user_id": user_id}  # a no-op move to the same owner
    for params in ({"user_id": user_id}, {}):
        ids = _owned_ids(client, user_id)
        r = client.request(method, url.format(**ids), params=params, json=body)
        assert r.status_code in (200, 201, 204), (params, r.text)


def test_reassign_document_moves_everything(
    client: TestClient, session: Session, user_id: int, other_user_id: int
) -> None:
    ids = _owned_ids(client, user_id)
    assert [s["user_id"] for s in _summary(client, user_ids=[user_id])] == [user_id]
    r = client.patch(
        f"/api/tax/documents/{ids['doc']}",
        params={"user_id": user_id},
        json={"user_id": other_user_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == other_user_id
    assert r.json()["warnings"] == ["Moved from alice."]
    assert _summary(client, user_ids=[user_id]) == []
    (moved,) = _summary(client, user_ids=[other_user_id])
    assert moved["total_income_minor"] == 100000 and moved["form_count"] == 1
    per_user = _summary(client, per_user=True)
    assert [(s["user_id"], s["total_income_minor"]) for s in per_user] == [(other_user_id, 100000)]
    session.expire_all()
    form = session.get(TaxForm, ids["form"])
    assert form is not None and form.user_id == other_user_id and form.is_user_edited is False
    assert {i.user_id for i in form.income_items} == {other_user_id}
    assert {t.user_id for t in form.tax_items} == {other_user_id}
    # The old owner can no longer reach it; the new one can.
    assert (
        client.get(f"/api/tax/documents/{ids['doc']}", params={"user_id": user_id}).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/tax/documents/{ids['doc']}", params={"user_id": other_user_id}
        ).status_code
        == 200
    )


def test_reassign_document_errors(client: TestClient, user_id: int) -> None:
    ids = _owned_ids(client, user_id)
    url = f"/api/tax/documents/{ids['doc']}"
    assert client.patch(url, json={"user_id": 9999}).status_code == 404
    assert client.patch(url, json={"user_id": user_id, "extra": 1}).status_code == 422
    assert client.patch(url, json={}).status_code == 422
    assert client.patch("/api/tax/documents/9999", json={"user_id": user_id}).status_code == 404


def _import_w2(session: Session, user: int, tmp_path: Path, tag: str) -> int:
    """Import the same W-2 content as a byte-different file (a different sha) for `user`."""
    path = tmp_path / f"w2_{tag}.pdf"
    path.write_bytes(generated_pdf("tax_w2", "normal").read_bytes() + f"\n% {tag}\n".encode())
    result = import_tax_document(session, path, user_id=user, original_name=path.name)
    session.commit()
    assert result.status == "parsed" and result.inserted_forms == 1
    return result.document_id


def test_reassign_collision_is_409_and_rolls_back(
    client: TestClient, session: Session, user_id: int, other_user_id: int, tmp_path: Path
) -> None:
    mine_doc = _import_w2(session, user_id, tmp_path, "scan-a")
    _import_w2(session, other_user_id, tmp_path, "scan-b")
    r = client.patch(f"/api/tax/documents/{mine_doc}", json={"user_id": other_user_id})
    assert r.status_code == 409
    assert "W-2" in r.json()["detail"] and "bob" in r.json()["detail"]
    session.expire_all()
    doc = session.get(TaxDocument, mine_doc)
    assert doc is not None and doc.user_id == user_id and doc.warnings is None
    form = doc.forms[0]
    assert form.user_id == user_id
    assert {i.user_id for i in form.income_items} == {user_id}


def test_reassign_rederives_fingerprints_so_later_imports_dedupe(
    client: TestClient, session: Session, user_id: int, other_user_id: int, tmp_path: Path
) -> None:
    doc_id = _import_w2(session, user_id, tmp_path, "scan-a")
    r = client.patch(f"/api/tax/documents/{doc_id}", json={"user_id": other_user_id})
    assert r.status_code == 200, r.text
    # Same content again for the new owner, from a different file: deduped by fingerprint.
    path = tmp_path / "w2_scan-c.pdf"
    path.write_bytes(generated_pdf("tax_w2", "normal").read_bytes() + b"\n% scan-c\n")
    again = import_tax_document(session, path, user_id=other_user_id, original_name=path.name)
    assert again.inserted_forms == 0 and again.skipped_duplicate_forms == 1
    # Alice can import it afresh (her copy left with the move).
    path2 = tmp_path / "w2_scan-d.pdf"
    path2.write_bytes(generated_pdf("tax_w2", "normal").read_bytes() + b"\n% scan-d\n")
    mine = import_tax_document(session, path2, user_id=user_id, original_name=path2.name)
    assert mine.inserted_forms == 1
    # The identical original file for the new owner is the same document.
    session.expire_all()
    doc = session.get(TaxDocument, doc_id)
    assert doc is not None
    original = import_tax_document(
        session, tmp_path / "w2_scan-a.pdf", user_id=other_user_id, original_name="x.pdf"
    )
    assert original.status == "duplicate" and original.document_id == doc_id


def test_reassign_manual_form_keeps_its_fingerprint(
    client: TestClient, session: Session, user_id: int, other_user_id: int
) -> None:
    ids = _owned_ids(client, user_id)
    form = session.get(TaxForm, ids["form"])
    assert form is not None
    before = form.fingerprint
    assert (
        client.patch(
            f"/api/tax/documents/{ids['doc']}", json={"user_id": other_user_id}
        ).status_code
        == 200
    )
    session.expire_all()
    form = session.get(TaxForm, ids["form"])
    assert form is not None and form.fingerprint == before and form.user_id == other_user_id
