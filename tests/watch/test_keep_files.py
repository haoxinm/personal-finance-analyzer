"""A file the user put in the documents folder stays theirs (never renamed, moved or deleted by the
app on its own), files the app filed itself are never imported a second time, an oversized file is
refused, and a scan yields to a restore between files."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from personal_finance_analyzer.api.services import crud, gateway
from personal_finance_analyzer.config import PrivacyConfig, Settings, save_settings
from personal_finance_analyzer.core import documents as docs
from personal_finance_analyzer.core.paths import documents_dir
from personal_finance_analyzer.db import documents as doc_files
from personal_finance_analyzer.db.models import User, WatchedFile
from personal_finance_analyzer.db.session import make_session_factory
from personal_finance_analyzer.ingest import pipeline
from personal_finance_analyzer.ingest.tax.pipeline import delete_tax_document, import_tax_document
from personal_finance_analyzer.watch import importers
from personal_finance_analyzer.watch import scanner as scanner_mod
from personal_finance_analyzer.watch.scanner import FolderScanner
from personal_finance_analyzer.watch.service import WatchService
from tests.fixtures.gen.base import render_lines_pdf
from tests.ingest.tax_support import generated_pdf
from tests.watch.support import (
    actions,
    add_bank,
    add_person,
    new_scanner,
    put_pdf_bytes,
    put_statement,
    put_tax,
    records,
    scan_stable,
    statement_pdf,
    statements,
    tax_documents,
)

STATEMENT = "Alex/Statements/Fixture Bank/jan.pdf"
TAX = "Alex/Tax/2024/W-2 - Pinecrest.pdf"


@pytest.fixture
def setup(session: Session, engine: Engine) -> FolderScanner:
    add_person(session, "Alex", default=True)
    add_bank(session)
    return new_scanner(engine)


def _touch(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))


def _delete_statement(session: Session, *, delete_file: bool = False) -> None:
    (statement,) = statements(session)
    in_place = statement.pdf_in_place
    stored_path, sha = crud.delete_statement(session, statement)
    session.commit()
    crud.discard_pdf_copy(session, stored_path, sha, in_place=in_place, delete_file=delete_file)


# ----------------------------------------------------------------- the user's file is theirs


def test_folder_imports_are_marked_as_the_users_own_file(
    setup: FolderScanner, session: Session
) -> None:
    put_statement(STATEMENT)
    put_tax(TAX)
    scan_stable(setup)
    (statement,) = statements(session)
    (document,) = tax_documents(session)
    assert statement.pdf_in_place is True and document.pdf_in_place is True


def test_deleting_a_statement_keeps_the_users_pdf_and_the_scan_leaves_it_alone(
    setup: FolderScanner, session: Session
) -> None:
    pdf = put_statement(STATEMENT)
    before = pdf.read_bytes()
    scan_stable(setup)

    _delete_statement(session)

    assert pdf.read_bytes() == before  # theirs: not deleted with the row
    assert statements(session) == []
    record = records(session)[STATEMENT]
    assert (record.outcome, record.code) == ("ignored", "deleted_in_app")
    report = scan_stable(setup)
    assert report.items == [] and statements(session) == []  # not imported again at once
    scan_stable(setup)
    assert statements(session) == []


def test_a_changed_file_is_imported_again_after_it_was_deleted_in_the_app(
    setup: FolderScanner, session: Session
) -> None:
    pdf = put_statement(STATEMENT)
    scan_stable(setup)
    _delete_statement(session)
    assert scan_stable(setup).items == []
    _touch(pdf)  # the user replaced / re-saved it
    assert actions(scan_stable(setup)) == {STATEMENT: "imported"}
    assert len(statements(session)) == 1


def test_import_again_clears_the_deleted_in_app_record(
    setup: FolderScanner, session: Session
) -> None:
    put_statement(STATEMENT)
    scan_stable(setup)
    _delete_statement(session)
    record = records(session)[STATEMENT]
    assert scanner_mod.retry(session, record.id) is True  # the button's call
    assert actions(scan_stable(setup)) == {STATEMENT: "imported"}


def test_delete_with_the_file_ticked_removes_the_pdf_and_leaves_no_record(
    setup: FolderScanner, session: Session
) -> None:
    pdf = put_statement(STATEMENT)
    scan_stable(setup)
    _delete_statement(session, delete_file=True)
    assert not pdf.exists()
    assert records(session)[STATEMENT].code != "deleted_in_app"  # nothing left to ignore


def test_deleting_a_tax_document_keeps_the_users_pdf_unless_asked(
    setup: FolderScanner, session: Session
) -> None:
    pdf = put_tax(TAX)
    scan_stable(setup)
    (document,) = tax_documents(session)
    assert delete_tax_document(session, document.id)
    assert pdf.is_file()
    assert records(session)[TAX].code == "deleted_in_app"
    assert scan_stable(setup).items == []

    put_tax(TAX)  # (same content again: a fresh import after "Import again")
    scanner_mod.retry(session, records(session)[TAX].id)
    assert actions(scan_stable(setup)) == {TAX: "imported"}
    (document,) = tax_documents(session)
    assert delete_tax_document(session, document.id, delete_pdf=True)
    assert not pdf.exists()


def test_the_apps_own_copy_is_still_deleted_with_its_row(
    setup: FolderScanner, session: Session, tmp_path: Path
) -> None:
    upload = tmp_path / "upload.pdf"
    shutil.copyfile(generated_pdf("layout_a_credit", "normal"), upload)
    user = session.scalars(select(User)).one()
    proposal = pipeline.propose_import(session, upload, user_id=user.id, original_name="u.pdf")
    assert proposal.issuer_id is not None
    pipeline.confirm_import(
        session,
        proposal.statement_id,
        issuer_id=proposal.issuer_id,
        parser_id=proposal.parser_id,
        layout_spec_id=proposal.layout_spec_id,
        remember=False,
    )
    session.commit()
    (statement,) = statements(session)
    assert statement.pdf_in_place is False and statement.stored_path is not None
    kept = documents_dir() / statement.stored_path
    assert kept.is_file()
    _delete_statement(session)
    assert not kept.exists()
    assert records(session) == {}


def test_reorganize_never_renames_a_file_the_user_put_there(
    setup: FolderScanner, session: Session
) -> None:
    pdf = put_statement(STATEMENT)
    scan_stable(setup)
    user = session.scalars(select(User)).one()
    user.name = "Alexandra"
    session.commit()
    report = doc_files.reorganize(session)
    assert report.changes == [] and report.missing == []
    assert pdf.is_file()
    (statement,) = statements(session)
    assert statement.stored_path == STATEMENT
    assert doc_files.file_statement(session, statement) == STATEMENT  # no rename either
    assert pdf.is_file()


def test_finishing_a_folder_import_on_the_import_page_does_not_rename_the_file(
    setup: FolderScanner, session: Session, tmp_path: Path
) -> None:
    rel = "Alex/Statements/Mystery/jan.pdf"
    pdf = statement_pdf(documents_dir() / rel, bank="Totally Unknown Credit Union")
    scan_stable(setup)
    (statement,) = statements(session)
    assert statement.status == "awaiting_extractor" and statement.pdf_in_place

    # The user uploads the same file on the Import page and picks an extractor.
    upload = tmp_path / "again.pdf"
    shutil.copyfile(pdf, upload)
    user = session.scalars(select(User)).one()
    proposal = pipeline.propose_import(session, upload, user_id=user.id, original_name="again.pdf")
    unknown = add_bank(session, "Totally Unknown Credit Union")
    pipeline.confirm_import(
        session,
        proposal.statement_id,
        issuer_id=unknown.id,
        parser_id=proposal.parser_id,
        layout_spec_id=proposal.layout_spec_id,
        remember=False,
    )
    session.commit()
    (statement,) = statements(session)
    assert statement.status == "parsed"
    assert statement.stored_path == rel and pdf.is_file()  # stays exactly where it was put
    assert sorted(
        p.relative_to(documents_dir()).as_posix() for p in documents_dir().rglob("*.pdf")
    ) == [rel]


# ----------------------------------------------------------- files the app filed itself


def test_a_failed_import_page_upload_without_text_is_not_imported_by_the_scan(
    setup: FolderScanner, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blank = tmp_path / "blank.pdf"
    render_lines_pdf(blank, [[]])
    user = session.scalars(select(User)).one()
    proposal = pipeline.propose_import(session, blank, user_id=user.id, original_name="blank.pdf")
    assert proposal.status == "no_text_layer"
    (statement,) = statements(session)
    assert statement.stored_path is not None and "/Unsorted/" in statement.stored_path
    assert statement.pdf_in_place is False

    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("the app's own filed copy must not be imported again")

    monkeypatch.setattr(gateway, "propose_import", boom)
    monkeypatch.setattr(importers, "import_statement", boom)
    report = scan_stable(setup)
    assert report.items == [] and report.scanned == 1
    assert records(session) == {}


def test_a_failed_tax_upload_with_copies_on_is_not_imported_by_the_scan(
    setup: FolderScanner, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_settings(Settings(privacy=PrivacyConfig(store_tax_pdf_copies=True)))
    user = session.scalars(select(User)).one()
    result = import_tax_document(
        session,
        generated_pdf("layout_a_credit", "normal"),  # not a tax form
        user_id=user.id,
        original_name="not a tax form.pdf",
    )
    assert result.status != "parsed"
    (doc,) = tax_documents(session)
    assert doc.stored_path is not None and doc.pdf_in_place is False

    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("the app's own filed copy must not be imported again")

    monkeypatch.setattr(importers, "import_tax", boom)
    report = scan_stable(setup)
    assert report.items == [] and report.scanned == 1


def test_retry_still_works_for_a_folder_import_that_failed(
    setup: FolderScanner, session: Session
) -> None:
    rel = "Alex/Statements/Mystery/jan.pdf"
    statement_pdf(documents_dir() / rel, bank="Totally Unknown Credit Union")
    assert actions(scan_stable(setup)) == {rel: "failed"}
    (statement,) = statements(session)
    assert statement.stored_path == rel  # the failed row names the user's file...
    assert scan_stable(setup).items == []  # ...and the record keeps it quiet
    assert scanner_mod.retry(session, records(session)[rel].id) is True
    assert actions(scan_stable(setup)) == {rel: "failed"}  # looked at again (not "known")
    assert len(statements(session)) == 1  # the same row, re-proposed


# ------------------------------------------------------------------------- size cap


def test_a_file_over_the_upload_limit_is_refused_before_it_is_read(
    setup: FolderScanner, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from personal_finance_analyzer.api.services.uploads import MAX_UPLOAD_BYTES

    assert scanner_mod.MAX_UPLOAD_BYTES == MAX_UPLOAD_BYTES == 25 * 1024 * 1024
    rel = "Alex/Statements/Bank/huge.pdf"
    body = b"%PDF-1.4\n" + b"0" * (26 * 1024 * 1024) + b"\n%%EOF\n"
    put_pdf_bytes(rel, body)

    def no_hashing(_path: Path) -> str | None:
        raise AssertionError("an oversized file must not be hashed")

    monkeypatch.setattr(docs, "file_sha256", no_hashing)
    monkeypatch.setattr(importers, "import_statement", no_hashing)
    report = scan_stable(setup)
    assert actions(report) == {rel: "failed"}
    record = records(session)[rel]
    assert record.code == "too_large"
    assert record.message is not None and "26 MB, limit 25 MB" in record.message
    assert statements(session) == [] and record.sha256 is None


def test_a_file_just_under_the_limit_is_not_refused(
    setup: FolderScanner, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scanner_mod, "MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
    put_statement(STATEMENT)
    assert actions(scan_stable(setup)) == {STATEMENT: "imported"}


# --------------------------------------------------------------- yielding to a restore


def _three_statements() -> list[str]:
    rels = [
        "Alex/Statements/Fixture Bank/a.pdf",
        "Alex/Statements/Fixture Bank/b.pdf",
        "Alex/Statements/Fixture Bank/c.pdf",
    ]
    for rel, variant in zip(rels, ("normal", "fx", "multiline"), strict=True):
        put_statement(rel, variant)
    return rels


def test_a_scan_stops_between_files_when_asked_and_resumes_without_waiting(
    setup: FolderScanner, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _three_statements()
    stop = {"now": False}
    real = importers.import_statement

    def import_then_stop(ctx: importers.Context) -> importers.Result:
        result = real(ctx)
        stop["now"] = True  # a restore starts while the first file is being imported
        return result

    monkeypatch.setattr(importers, "import_statement", import_then_stop)
    first = setup.scan(settle_seconds=1.0, should_stop=lambda: stop["now"])
    assert first.interrupted is True and first.imported == 1
    assert len(statements(session)) == 1

    stop["now"] = False
    monkeypatch.setattr(importers, "import_statement", real)
    second = setup.scan()  # no settle pause: the unhandled files were already "stable"
    assert second.interrupted is False and second.imported == 2
    assert len(statements(session)) == 3


def test_a_scan_asked_to_stop_before_it_starts_does_nothing(
    setup: FolderScanner, session: Session
) -> None:
    _three_statements()
    report = setup.scan(settle_seconds=1.0, should_stop=lambda: True)
    assert report.interrupted is True and report.items == []
    assert statements(session) == [] and records(session) == {}


def test_the_service_hands_its_pause_flag_to_the_scan(
    engine: Engine, session: Session, setup: FolderScanner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _three_statements()
    factory = make_session_factory(engine)
    paused = {"now": False}
    scanner = FolderScanner(factory, sleep=lambda _s: None)
    service = WatchService(engine, factory, scanner=scanner, paused=lambda: paused["now"])
    real = importers.import_statement

    def import_then_pause(ctx: importers.Context) -> importers.Result:
        result = real(ctx)
        paused["now"] = True  # a restore takes the data directory while the first file imports
        return result

    monkeypatch.setattr(importers, "import_statement", import_then_pause)
    assert service.run_scan(manual=True) is not None
    assert len(statements(session)) == 1  # it yielded after one file, not after all three
    assert service.wait_idle(5.0) is True


def test_nothing_is_ignored_when_the_file_is_already_gone(
    setup: FolderScanner, session: Session
) -> None:
    put_statement(STATEMENT)
    scan_stable(setup)
    (statement,) = statements(session)
    path = documents_dir() / STATEMENT
    stored, sha, in_place = statement.stored_path, statement.file_sha256, statement.pdf_in_place
    path.unlink()  # the file is gone too: nothing to ignore
    session.delete(statement)
    session.commit()
    assert stored is not None
    doc_files.remove_row_file(session, stored, sha, in_place=in_place)
    assert not list(
        session.scalars(select(WatchedFile).where(WatchedFile.code == "deleted_in_app"))
    )
