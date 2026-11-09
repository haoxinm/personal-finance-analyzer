"""The readable documents folder through the real import pipelines and the API: where a confirmed
statement or tax form ends up, reading it after the user moved or deleted it by hand ("PDF not
found"), and what deleting a row does to the file."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from personal_finance_analyzer.config import PrivacyConfig, Settings, save_settings
from personal_finance_analyzer.core import documents as docs
from personal_finance_analyzer.core.paths import documents_dir, staging_dir
from personal_finance_analyzer.db.models import Issuer, Statement, TaxDocument, User
from personal_finance_analyzer.ingest import pipeline
from personal_finance_analyzer.ingest.tax.pipeline import import_tax_document
from tests.fixtures.gen.base import format_row, render_lines_pdf
from tests.ingest.tax_support import generated_pdf


def _statement_pdf(path: Path) -> None:
    widths = (10, 32, 14)
    lines = [
        "Previous Balance: $0.00",
        "New Balance: $87.43",
        "Statement Period: 01/01/2026 to 01/31/2026",
        "",
        format_row(["Date", "Description", "Amount"], widths),
        format_row(["01/03/2026", "GROCERY MART", "45.10"], widths),
        format_row(["01/05/2026", "COFFEE SHOP", "12.34"], widths),
        format_row(["01/09/2026", "ONLINE BOOKSTORE", "29.99"], widths),
    ]
    render_lines_pdf(path, [lines])


def _import_statement(session: Session, tmp_path: Path, *, keep: bool = True) -> Statement:
    save_settings(Settings(privacy=PrivacyConfig(store_pdf_copies=keep)))
    user = session.query(User).first() or User(name="Alex", is_default=True)
    issuer = Issuer(name="Old Bank", slug="old-bank")
    session.add_all([user, issuer])
    session.flush()
    source = tmp_path / "statement.pdf"
    _statement_pdf(source)
    proposal = pipeline.propose_import(session, source, user_id=user.id, original_name="jan.pdf")
    pipeline.confirm_import(
        session,
        proposal.statement_id,
        issuer_id=issuer.id,
        parser_id=proposal.parser_id,
        layout_spec_id=proposal.layout_spec_id,
        remember=False,
    )
    session.commit()
    statement = session.get(Statement, proposal.statement_id)
    assert statement is not None
    return statement


def test_a_confirmed_statement_is_kept_under_a_readable_name(
    session: Session, tmp_path: Path
) -> None:
    statement = _import_statement(session, tmp_path)
    assert statement.stored_path is not None
    person, area, institution, name = statement.stored_path.split("/")
    assert (person, area, institution) == ("Alex", "Statements", "Old Bank")
    assert name.startswith("2026-01 Old Bank") and name.endswith(".pdf")
    assert (documents_dir() / statement.stored_path).is_file()
    # The hash-named upload is gone: one readable copy, nothing else to keep in sync.
    assert list(staging_dir().glob("*.pdf")) == []
    assert statement.status == "parsed"


def test_nothing_is_kept_when_pdf_copies_are_off(session: Session, tmp_path: Path) -> None:
    statement = _import_statement(session, tmp_path, keep=False)
    assert statement.stored_path is None
    assert list(staging_dir().glob("*.pdf")) == []
    assert not documents_dir().exists() or list(documents_dir().rglob("*.pdf")) == []


def test_pages_still_load_after_the_user_moves_or_renames_the_file(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement = _import_statement(session, tmp_path)
    assert statement.stored_path is not None
    assert client.get(f"/api/statements/{statement.id}/pages").status_code == 200
    mine = documents_dir() / "My own folder" / "january (final).pdf"
    mine.parent.mkdir()
    (documents_dir() / statement.stored_path).rename(mine)
    response = client.get(f"/api/statements/{statement.id}/pages/1/image?width=200")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert client.get(f"/api/statements/{statement.id}/pages").status_code == 200


def test_a_deleted_file_is_reported_as_not_found_not_an_error(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement = _import_statement(session, tmp_path)
    assert statement.stored_path is not None
    (documents_dir() / statement.stored_path).unlink()
    for url in (
        f"/api/statements/{statement.id}/pages",
        f"/api/statements/{statement.id}/pages/1/image",
        f"/api/statements/{statement.id}/preview?page=1",
    ):
        response = client.get(url)
        assert response.status_code == 404, url
        detail = response.json()["detail"]
        assert detail["code"] == "pdf_not_found"
        assert "PDF not found" in detail["message"]


def test_a_statement_that_never_kept_a_copy_says_so_differently(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement = _import_statement(session, tmp_path, keep=False)
    detail = client.get(f"/api/statements/{statement.id}/pages").json()["detail"]
    assert detail["code"] == "pdf_not_kept"


def test_deleting_a_statement_removes_its_pdf_and_the_folders_it_leaves(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement = _import_statement(session, tmp_path)
    assert statement.stored_path is not None
    path = documents_dir() / statement.stored_path
    assert client.delete(f"/api/statements/{statement.id}").status_code == 204
    assert not path.exists() and not path.parent.exists()
    assert documents_dir().is_dir()


def test_deleting_a_statement_keeps_a_pdf_another_row_still_uses(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement = _import_statement(session, tmp_path)
    assert statement.stored_path is not None
    session.add(
        TaxDocument(
            user_id=statement.user_id,
            file_sha256=statement.file_sha256,
            original_name="same-file.pdf",
            status="parsed",
            stored_path=statement.stored_path,
        )
    )
    session.commit()
    path = documents_dir() / statement.stored_path
    assert client.delete(f"/api/statements/{statement.id}").status_code == 204
    assert path.is_file()  # the tax document row still refers to this very file


def test_reuploading_a_confirmed_statement_leaves_nothing_behind(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    statement = _import_statement(session, tmp_path)
    assert statement.stored_path is not None
    response = client.post(
        "/api/statements",
        data={"user_id": str(statement.user_id)},
        files={"file": ("again.pdf", (tmp_path / "statement.pdf").read_bytes(), "application/pdf")},
    )
    assert response.status_code == 201 and response.json()["duplicate"] is True
    assert list(staging_dir().glob("*.pdf")) == []  # no stray hash-named copy
    assert len(list(documents_dir().rglob("*.pdf"))) == 1
    assert (documents_dir() / statement.stored_path).is_file()


def test_reparse_says_so_when_the_pdf_is_gone(session: Session, tmp_path: Path) -> None:
    from personal_finance_analyzer.core.errors import PfaError

    statement = _import_statement(session, tmp_path)
    assert statement.stored_path is not None
    (documents_dir() / statement.stored_path).unlink()
    with pytest.raises(PfaError, match="not found in the documents folder"):
        pipeline.reparse(session, statement.id, parser_id="generic_table", layout_spec_id=None)


# --- tax documents ------------------------------------------------------------------------------


def _import_tax(session: Session) -> TaxDocument:
    save_settings(Settings(privacy=PrivacyConfig(store_tax_pdf_copies=True)))
    user = session.query(User).first() or User(name="Alex", is_default=True)
    session.add(user)
    session.flush()
    result = import_tax_document(
        session, generated_pdf("tax_w2", "normal"), user_id=user.id, original_name="w2.pdf"
    )
    document = session.get(TaxDocument, result.document_id)
    assert document is not None
    return document


def test_a_tax_document_reports_a_missing_pdf(client: TestClient, session: Session) -> None:
    document = _import_tax(session)
    assert document.stored_path is not None
    listed = client.get("/api/tax/documents").json()[0]
    assert listed["has_pdf"] is True and listed["pdf_missing"] is False

    (documents_dir() / document.stored_path).unlink()
    listed = client.get("/api/tax/documents").json()[0]
    assert listed["has_pdf"] is False and listed["pdf_missing"] is True
    response = client.get(f"/api/tax/documents/{document.id}/pages")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "pdf_needed"
    assert "PDF not found" in response.json()["detail"]["message"]


def test_a_moved_tax_pdf_is_still_found(client: TestClient, session: Session) -> None:
    document = _import_tax(session)
    assert document.stored_path is not None
    moved = documents_dir() / "scans" / "w2.pdf"
    moved.parent.mkdir()
    (documents_dir() / document.stored_path).rename(moved)
    # The list only looks at the recorded path (it must never walk a share): not found yet...
    listed = client.get("/api/tax/documents").json()[0]
    assert listed["has_pdf"] is False and listed["pdf_missing"] is True
    # ...opening the PDF searches by content and finds it, and the list knows from then on.
    assert client.get(f"/api/tax/documents/{document.id}/pages").status_code == 200
    listed = client.get("/api/tax/documents").json()[0]
    assert listed["has_pdf"] is True and listed["pdf_missing"] is False


def test_listing_tax_documents_never_searches_the_documents_folder(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _import_tax(session)
    assert document.stored_path is not None
    (documents_dir() / document.stored_path).unlink()

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("the list walked the documents folder")

    monkeypatch.setattr(docs, "iter_files", boom)
    monkeypatch.setattr(docs, "find_by_sha", boom)
    listed = client.get("/api/tax/documents").json()[0]
    assert listed["has_pdf"] is False and listed["pdf_missing"] is True


def test_a_tax_document_without_a_kept_copy_is_not_reported_missing(
    client: TestClient, session: Session
) -> None:
    save_settings(Settings())
    user = User(name="Alex", is_default=True)
    session.add(user)
    session.flush()
    import_tax_document(
        session, generated_pdf("tax_w2", "normal"), user_id=user.id, original_name="w2.pdf"
    )
    listed = client.get("/api/tax/documents").json()[0]
    assert listed["has_pdf"] is False and listed["pdf_missing"] is False


# The same bytes docker/smoke-test.sh uploads: a one-page PDF with no text layer.
_BLANK_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R/Size 4>>\n"
    b"%%EOF\n"
)


def test_an_uploaded_pdf_without_text_lands_at_a_readable_path(
    client: TestClient, session: Session
) -> None:
    user = User(name="Persisted", is_default=True)
    session.add(user)
    session.commit()
    response = client.post(
        "/api/statements",
        data={"user_id": str(user.id)},
        files={"file": ("blank 4111111111111111.pdf", _BLANK_PDF, "application/pdf")},
    )
    assert response.status_code == 201, response.text
    kept = sorted(documents_dir().rglob("*.pdf"))
    assert len(kept) == 1
    relative = kept[0].relative_to(documents_dir())
    assert relative.parts[:3] == ("Persisted", "Statements", "Unsorted")
    assert " Statement " in relative.name and "4111111111111111" not in str(relative)
    assert kept[0].read_bytes() == _BLANK_PDF
    assert list(staging_dir().glob("*.pdf")) == []
