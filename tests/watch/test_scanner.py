"""One scan of the documents folder, through the real import pipelines on a real database."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from personal_finance_analyzer.api.services import gateway
from personal_finance_analyzer.config import LLMConfig, PrivacyConfig, Settings, save_settings
from personal_finance_analyzer.core.filelock import FileLock
from personal_finance_analyzer.core.paths import documents_dir
from personal_finance_analyzer.db.models import Transaction
from personal_finance_analyzer.db.session import make_session_factory
from personal_finance_analyzer.ingest import pipeline
from personal_finance_analyzer.watch import scanner as scanner_mod
from personal_finance_analyzer.watch.scanner import FolderScanner
from tests.ingest.tax_support import generated_pdf
from tests.llm_fakes import FakeLLM, install
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
    statements,
    tax_documents,
)

STATEMENT = "Alex/Statements/Fixture Bank/jan.pdf"


@pytest.fixture
def setup(session: Session, engine: Engine) -> FolderScanner:
    add_person(session, "Alex", default=True)
    add_bank(session)
    return new_scanner(engine)


# ---------------------------------------------------------------------------------- importing


def test_a_statement_in_a_person_folder_is_imported_where_it_is(
    setup: FolderScanner, session: Session
) -> None:
    pdf = put_statement(STATEMENT)
    before = pdf.read_bytes()
    report = scan_stable(setup)
    assert actions(report) == {STATEMENT: "imported"}, report.items
    (statement,) = statements(session)
    assert statement.status == "parsed" and (statement.txn_count or 0) > 0
    assert statement.user_id is not None
    # Recorded where the user put it: not renamed, not moved, not copied.
    assert statement.stored_path == STATEMENT
    assert pdf.read_bytes() == before
    assert sorted(
        p.relative_to(documents_dir()).as_posix() for p in documents_dir().rglob("*.pdf")
    ) == [STATEMENT]
    record = records(session)[STATEMENT]
    assert (record.outcome, record.kind, record.statement_id) == (
        "imported",
        "statement",
        statement.id,
    )
    assert record.sha256 == statement.file_sha256


def test_the_imported_statement_is_filed_under_its_person_and_classified(
    setup: FolderScanner, session: Session
) -> None:
    put_statement(STATEMENT)
    scan_stable(setup)
    session.expire_all()
    (statement,) = statements(session)
    assert statement.account_id is not None
    rows = list(session.scalars(select(Transaction)))
    assert rows
    assert all(r.user_id == statement.user_id for r in rows)
    assert all(r.category_id is not None or r.needs_review for r in rows)


def test_a_rescan_does_nothing_more(setup: FolderScanner, session: Session) -> None:
    put_statement(STATEMENT)
    scan_stable(setup)
    again = scan_stable(setup)
    assert again.items == [] and again.scanned == 1
    assert len(statements(session)) == 1


def test_a_tax_form_is_imported_for_the_person_and_kept_in_place(
    setup: FolderScanner, session: Session
) -> None:
    rel = "Alex/Tax/2024/W-2 - Pinecrest.pdf"
    put_tax(rel)
    report = scan_stable(setup)
    assert actions(report) == {rel: "imported"}, report.items
    (doc,) = tax_documents(session)
    assert doc.status == "parsed" and doc.tax_year == 2024
    assert doc.stored_path == rel
    assert (documents_dir() / rel).is_file()


def test_a_tax_form_is_recorded_even_though_the_app_does_not_copy_tax_pdfs(
    setup: FolderScanner, session: Session
) -> None:
    save_settings(Settings(privacy=PrivacyConfig(store_tax_pdf_copies=False)))
    rel = "Alex/Tax/2024/w2.pdf"
    put_tax(rel)
    scan_stable(setup)
    (doc,) = tax_documents(session)
    assert doc.stored_path == rel  # the setting governs copies of uploads, not this file


def test_the_forms_own_year_wins_and_a_folder_mismatch_is_a_warning(
    setup: FolderScanner, session: Session
) -> None:
    rel = "Alex/Tax/2021/W-2.pdf"  # the form says 2024
    put_tax(rel)
    report = scan_stable(setup)
    assert actions(report) == {rel: "imported"}  # not a failure
    (doc,) = tax_documents(session)
    assert doc.tax_year == 2024
    record = records(session)[rel]
    assert record.outcome == "imported"
    assert record.warnings is not None and "2024" in record.warnings and "2021" in record.warnings


def test_the_folder_year_fills_in_a_year_the_form_does_not_print(
    setup: FolderScanner, session: Session
) -> None:
    rel = "Alex/Tax/2023/W-2.pdf"
    put_tax(rel, "no_year")
    report = scan_stable(setup)
    assert actions(report) == {rel: "imported"}, report.items
    (doc,) = tax_documents(session)
    assert doc.tax_year == 2023


def test_nested_folders_and_case_insensitive_person_names(
    setup: FolderScanner, session: Session
) -> None:
    rel = "ALEX/statements/Chase/Checking/2026/jan.pdf"
    put_statement(rel)
    assert actions(scan_stable(setup)) == {rel: "imported"}
    assert statements(session)[0].stored_path == rel


# ---------------------------------------------------------------------------------- stability


def test_a_new_file_waits_for_a_second_scan_without_a_settle_pause(
    setup: FolderScanner, session: Session
) -> None:
    put_statement(STATEMENT)
    first = setup.scan()
    assert actions(first) == {STATEMENT: "waiting"}
    assert statements(session) == []
    second = setup.scan()
    assert actions(second) == {STATEMENT: "imported"}


def test_a_file_that_changes_between_scans_keeps_waiting(
    setup: FolderScanner, session: Session
) -> None:
    pdf = put_statement(STATEMENT)
    setup.scan()
    data = pdf.read_bytes()
    pdf.write_bytes(data + b"\n")  # still being written to: size changed
    assert actions(setup.scan()) == {STATEMENT: "waiting"}
    assert statements(session) == []
    assert actions(setup.scan()) == {STATEMENT: "imported"}


def test_a_file_that_changes_during_the_settle_pause_waits(
    engine: Engine, session: Session
) -> None:
    add_person(session, "Alex")
    add_bank(session)
    pdf = put_statement(STATEMENT)

    def grow(_seconds: float) -> None:
        pdf.write_bytes(pdf.read_bytes() + b"\n")

    scanner = FolderScanner(make_session_factory(engine), sleep=grow)
    assert actions(scanner.scan(settle_seconds=1.0)) == {STATEMENT: "waiting"}
    assert statements(session) == []


def test_a_half_written_pdf_waits_even_when_its_size_is_steady(
    setup: FolderScanner, session: Session
) -> None:
    pdf = put_statement(STATEMENT)
    whole = pdf.read_bytes()
    pdf.write_bytes(whole[: len(whole) // 2])  # no %%EOF yet
    setup.scan()
    report = setup.scan()
    assert actions(report) == {STATEMENT: "waiting"}
    assert report.items[0].code == "incomplete_pdf"
    assert statements(session) == [] and records(session) == {}
    pdf.write_bytes(whole)  # the copy finishes
    setup.scan()
    assert actions(setup.scan()) == {STATEMENT: "imported"}


def test_a_pdf_that_never_finishes_is_eventually_reported(engine: Engine, session: Session) -> None:
    add_person(session, "Alex")
    clock = [1_000_000.0]
    scanner = FolderScanner(
        make_session_factory(engine), clock=lambda: clock[0], sleep=lambda _s: None
    )
    pdf = put_pdf_bytes(STATEMENT, b"%PDF-1.4 truncated")
    os.utime(pdf, (clock[0], clock[0]))
    scanner.scan()
    assert actions(scanner.scan()) == {STATEMENT: "waiting"}
    clock[0] += scanner_mod.INCOMPLETE_AFTER_S + 1
    report = scanner.scan()
    assert actions(report) == {STATEMENT: "failed"}
    assert records(session)[STATEMENT].code == "incomplete_pdf"


# ---------------------------------------------------------------------------------- ignored


def test_hidden_temporary_and_non_pdf_files_are_ignored_entirely(
    setup: FolderScanner, session: Session
) -> None:
    base = "Alex/Statements/Invented Card Co"
    whole = put_statement("scratch.pdf").read_bytes()
    (documents_dir() / "scratch.pdf").unlink()
    for rel in (
        f"{base}/.hidden.pdf",
        f"{base}/~$lock.pdf",
        f"{base}/jan.pdf.part",
        f"{base}/notes.txt",
        f"{base}/.pfa-restore-1/x.pdf",
        "README.txt",
    ):
        put_pdf_bytes(rel, whole)
    report = scan_stable(setup)
    assert report.items == [] and report.scanned == 0
    assert records(session) == {}


def test_the_staging_folder_is_never_scanned(
    setup: FolderScanner, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `incoming/` inside the documents folder (a data dir used as the documents folder).
    monkeypatch.setenv("PFA_DOCUMENTS_DIR", str(tmp_path / "pfa_home"))
    staged = tmp_path / "pfa_home" / "incoming"
    put_statement("x.pdf")  # lands in the (new) documents folder; move it into incoming
    staged.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp_path / "pfa_home" / "x.pdf"), staged / "Alex.pdf")
    assert scan_stable(setup).items == []


def test_a_pdf_outside_any_person_and_type_folder_is_listed_as_not_recognised(
    setup: FolderScanner, session: Session
) -> None:
    put_statement("loose.pdf")
    put_statement("Alex/Receipts/r.pdf")
    report = scan_stable(setup)
    assert actions(report) == {"loose.pdf": "ignored", "Alex/Receipts/r.pdf": "ignored"}
    recs = records(session)
    assert {r.code for r in recs.values()} == {"not_recognised_folder"}
    assert statements(session) == []


# ---------------------------------------------------------------------------------- people


def test_an_unknown_person_is_reported_and_never_created(
    setup: FolderScanner, session: Session
) -> None:
    rel = "Taylor/Statements/Invented Card Co/jan.pdf"
    put_statement(rel)
    report = scan_stable(setup)
    assert actions(report) == {rel: "failed"}
    record = records(session)[rel]
    assert record.code == "unknown_person" and record.folder_person == "Taylor"
    assert record.hint is not None and "Taylor" in record.hint
    assert statements(session) == []
    from personal_finance_analyzer.db.models import User

    assert [u.name for u in session.scalars(select(User))] == ["Alex"]


def test_a_person_created_later_is_picked_up_without_a_retry(
    setup: FolderScanner, session: Session
) -> None:
    rel = "Taylor/Statements/Invented Card Co/jan.pdf"
    put_statement(rel)
    scan_stable(setup)
    assert statements(session) == []
    add_person(session, "taylor")
    assert actions(scan_stable(setup)) == {rel: "imported"}
    assert statements(session)[0].stored_path == rel
    assert records(session)[rel].outcome == "imported"


def test_shared_cannot_be_imported_without_a_person(setup: FolderScanner, session: Session) -> None:
    rel = "Shared/Statements/jan.pdf"
    put_statement(rel)
    assert actions(scan_stable(setup)) == {rel: "failed"}
    assert records(session)[rel].code == "shared_needs_person"
    assert statements(session) == []


# ---------------------------------------------------------------------------------- dedupe


def test_files_the_app_filed_itself_are_skipped_without_being_read(
    setup: FolderScanner, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = tmp_path / "upload.pdf"
    shutil.copyfile(generated_pdf("layout_a_credit", "normal"), upload)
    from personal_finance_analyzer.db.models import User

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
    assert statement.stored_path and statement.stored_path.startswith("Alex/Statements/")

    from personal_finance_analyzer.core import documents as docs

    def no_hashing(_path: Path) -> str | None:
        raise AssertionError("an already-known file must not be hashed")

    monkeypatch.setattr(docs, "file_sha256", no_hashing)
    report = scan_stable(setup)
    assert report.items == [] and report.scanned == 1
    assert records(session) == {}


def test_a_copy_of_an_imported_file_in_the_same_persons_folder_is_known(
    setup: FolderScanner, session: Session
) -> None:
    pdf = put_statement(STATEMENT)
    scan_stable(setup)
    copy_rel = "Alex/Statements/Other/copy.pdf"
    (documents_dir() / copy_rel).parent.mkdir(parents=True)
    shutil.copyfile(pdf, documents_dir() / copy_rel)
    report = scan_stable(setup)
    assert actions(report) == {copy_rel: "known"}
    assert len(statements(session)) == 1
    assert statements(session)[0].stored_path == STATEMENT  # the original stays the record


def test_the_same_content_in_two_persons_folders_reports_the_second(
    setup: FolderScanner, session: Session
) -> None:
    add_person(session, "Sam")
    pdf = put_statement(STATEMENT)
    scan_stable(setup)
    other = "Sam/Statements/Invented Card Co/jan.pdf"
    (documents_dir() / other).parent.mkdir(parents=True)
    shutil.copyfile(pdf, documents_dir() / other)
    report = scan_stable(setup)
    assert actions(report) == {other: "failed"}
    record = records(session)[other]
    assert record.code == "duplicate_other_person"
    assert record.message is not None and "Alex" in record.message
    assert len(statements(session)) == 1 and statements(session)[0].stored_path == STATEMENT


def test_a_moved_file_is_recorded_as_the_pdf_of_its_row(
    setup: FolderScanner, session: Session
) -> None:
    pdf = put_statement(STATEMENT)
    scan_stable(setup)
    moved = "Alex/Statements/Invented Card Co/2026/january.pdf"
    (documents_dir() / moved).parent.mkdir(parents=True)
    pdf.rename(documents_dir() / moved)
    report = scan_stable(setup)
    assert actions(report) == {moved: "known"}
    assert statements(session)[0].stored_path == moved


def test_a_tax_form_in_the_other_persons_folder_is_reported_too(
    setup: FolderScanner, session: Session
) -> None:
    add_person(session, "Sam")
    mine = put_tax("Alex/Tax/2024/w2.pdf")
    scan_stable(setup)
    other = "Sam/Tax/2024/w2.pdf"
    (documents_dir() / other).parent.mkdir(parents=True)
    shutil.copyfile(mine, documents_dir() / other)
    assert actions(scan_stable(setup)) == {other: "failed"}
    assert records(session)[other].code == "duplicate_other_person"
    assert len(tax_documents(session)) == 1


# ---------------------------------------------------------------------------------- failures


def _count_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = gateway.propose_import

    def counting(*args: object, **kwargs: object) -> object:
        calls.append("propose")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gateway, "propose_import", counting)
    return calls


def test_a_failure_is_recorded_and_not_retried_until_the_file_changes(
    setup: FolderScanner, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_calls(monkeypatch)
    rel = "Alex/Statements/Bank/broken.pdf"
    put_pdf_bytes(rel, b"%PDF-1.4\nthis is not a real pdf body\n%%EOF\n")
    report = scan_stable(setup)
    assert actions(report) == {rel: "failed"}, report.items
    record = records(session)[rel]
    assert record.code == "unreadable" and record.message
    assert record.first_seen == record.last_seen or record.last_seen >= record.first_seen
    assert len(calls) == 1

    scan_stable(setup)
    scan_stable(setup)
    assert len(calls) == 1  # unchanged file: not looked at again

    path = documents_dir() / rel
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))  # touched
    scan_stable(setup)
    assert len(calls) == 2  # changed mtime: tried again


def test_a_changed_size_is_tried_again_and_can_then_succeed(
    setup: FolderScanner, session: Session
) -> None:
    rel = STATEMENT
    put_pdf_bytes(rel, b"%PDF-1.4\nnot a real pdf\n%%EOF\n")
    assert actions(scan_stable(setup)) == {rel: "failed"}
    put_statement(rel)  # the user replaces it with the real statement
    assert actions(scan_stable(setup)) == {rel: "imported"}
    record = records(session)[rel]
    assert record.outcome == "imported" and record.code is None


def test_retry_clears_the_record_so_the_file_is_tried_again(
    setup: FolderScanner, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_calls(monkeypatch)
    rel = "Alex/Statements/Bank/broken.pdf"
    put_pdf_bytes(rel, b"%PDF-1.4\nnot a real pdf\n%%EOF\n")
    scan_stable(setup)
    record_id = records(session)[rel].id
    assert len(calls) == 1
    assert scanner_mod.retry(session, record_id) is True
    assert rel not in records(session)
    scan_stable(setup)
    assert len(calls) == 2
    assert scanner_mod.retry(session, 99999) is False


def test_retry_only_applies_to_problems(setup: FolderScanner, session: Session) -> None:
    put_statement(STATEMENT)
    scan_stable(setup)
    record = records(session)[STATEMENT]
    assert scanner_mod.retry(session, record.id) is False


def test_a_failed_file_that_disappears_is_forgotten(setup: FolderScanner, session: Session) -> None:
    rel = "Taylor/Statements/x.pdf"
    pdf = put_statement(rel)
    scan_stable(setup)
    assert rel in records(session)
    pdf.unlink()
    scan_stable(setup)
    assert records(session) == {}


def test_an_unrecognised_statement_layout_is_reported_with_the_way_forward(
    setup: FolderScanner, session: Session
) -> None:
    from tests.watch.support import statement_pdf

    rel = "Alex/Statements/Mystery/jan.pdf"
    statement_pdf(documents_dir() / rel, bank="Totally Unknown Credit Union")
    report = scan_stable(setup)
    assert actions(report) == {rel: "failed"}
    record = records(session)[rel]
    assert record.code == "needs_extractor"
    assert record.hint is not None and "Import" in record.hint
    (statement,) = statements(session)
    assert statement.status == "awaiting_extractor" and statement.stored_path == rel
    assert statement.txn_count is None


def test_a_statement_finished_by_hand_later_turns_the_failure_into_an_import(
    setup: FolderScanner, session: Session
) -> None:
    from tests.watch.support import statement_pdf

    rel = "Alex/Statements/Mystery/jan.pdf"
    statement_pdf(documents_dir() / rel, bank="Totally Unknown Credit Union")
    scan_stable(setup)
    (statement,) = statements(session)
    unknown = add_bank(session, "Totally Unknown Credit Union")
    pipeline.confirm_import(
        session,
        statement.id,
        issuer_id=unknown.id,
        parser_id=statement.parser_id,
        layout_spec_id=statement.layout_spec_id,
        remember=False,
        keep_in_place=True,
    )
    session.commit()
    scan_stable(setup)
    assert records(session)[rel].outcome == "imported"


def test_a_scan_that_hits_an_unexpected_error_records_it_and_goes_on(
    setup: FolderScanner, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("secret detail that must not be stored")

    monkeypatch.setattr(gateway, "propose_import", boom)
    put_statement(STATEMENT)
    put_tax("Alex/Tax/2024/w2.pdf")
    report = scan_stable(setup)
    assert actions(report)[STATEMENT] == "failed"
    assert actions(report)["Alex/Tax/2024/w2.pdf"] == "imported"
    record = records(session)[STATEMENT]
    assert record.code == "error" and "secret detail" not in (record.message or "")


# ---------------------------------------------------------------------------------- the lock


def test_a_scan_does_nothing_while_another_holds_the_lock(
    setup: FolderScanner, session: Session
) -> None:
    put_statement(STATEMENT)
    holder = FileLock(scanner_mod.lock_path())
    assert holder.try_acquire()
    try:
        report = scan_stable(setup)
        assert report.busy is True and report.items == []
        assert statements(session) == []
    finally:
        holder.release()
    assert actions(scan_stable(setup)) == {STATEMENT: "imported"}


def test_two_scanners_cannot_import_at_once(
    engine: Engine, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_person(session, "Alex")
    add_bank(session)
    put_statement(STATEMENT)
    second = new_scanner(engine)
    seen: list[bool] = []
    real = gateway.propose_import

    def nested(*args: object, **kwargs: object) -> object:
        seen.append(second.scan(settle_seconds=1.0).busy)  # while the first scan holds the lock
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gateway, "propose_import", nested)
    first = new_scanner(engine)
    assert actions(first.scan(settle_seconds=1.0)) == {STATEMENT: "imported"}
    assert seen == [True]


# ---------------------------------------------------------------------------------- dry run


def test_a_dry_run_changes_nothing(setup: FolderScanner, session: Session) -> None:
    put_statement(STATEMENT)
    put_tax("Alex/Tax/2024/w2.pdf")
    put_statement("Taylor/Statements/x.pdf")
    before = sorted(p.relative_to(documents_dir()).as_posix() for p in documents_dir().rglob("*"))
    report = setup.scan(dry_run=True, settle_seconds=1.0)
    assert report.dry_run and report.would_import == 2
    assert actions(report)["Taylor/Statements/x.pdf"] == "failed"
    assert statements(session) == [] and tax_documents(session) == [] and records(session) == {}
    assert (
        sorted(p.relative_to(documents_dir()).as_posix() for p in documents_dir().rglob("*"))
        == before
    )
    # ...and a real scan afterwards still imports them.
    real = scan_stable(setup)
    assert real.imported == 2


# ---------------------------------------------------------------------------------- the LLM


def _llm_settings() -> Settings:
    return Settings(
        llm=LLMConfig(
            enabled=True,
            base_url="https://llm.example.com",
            api_format="openai_chat",
            model="test-model",
        )
    )


def test_without_an_llm_nothing_is_sent_anywhere(
    setup: FolderScanner, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM())
    put_statement(STATEMENT)
    assert actions(scan_stable(setup)) == {STATEMENT: "imported"}
    assert fake.requests == []


def test_with_an_llm_enabled_the_descriptions_are_sent_without_a_click(
    setup: FolderScanner, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_settings(_llm_settings())
    fake = install(monkeypatch, FakeLLM(default="[]"))
    put_statement(STATEMENT)
    report = scan_stable(setup)
    assert actions(report) == {STATEMENT: "imported"}, report.items
    assert fake.requests, "the folder import must go through the same classification as an upload"
    assert "ZQXV" in str(fake.requests[0]).upper() or fake.requests
    assert fake.urls[0].startswith("https://llm.example.com")
