"""Which file belongs to which row: filing after an import, deleting, the one-time carry-over of the
old hash-named ``statements/`` folder (idempotent, crash-safe), ``reorganize`` and ``check``."""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from personal_finance_analyzer.core import documents as docs
from personal_finance_analyzer.core.paths import documents_dir, legacy_statements_dir, staging_dir
from personal_finance_analyzer.core.tax_types import FORM_ROLE
from personal_finance_analyzer.db import documents as doc_files
from personal_finance_analyzer.db.models import (
    Account,
    Issuer,
    Statement,
    TaxDocument,
    TaxForm,
    User,
)

_counter = itertools.count(1)


def _pdf(tmp_path: Path, tag: str | None = None) -> tuple[Path, str]:
    tag = tag or f"pdf{next(_counter)}"
    data = b"%PDF-1.4\n" + tag.encode() * 64
    path = tmp_path / "uploads" / f"{tag}.pdf"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


@dataclass
class World:
    alice: User
    bob: User
    issuer: Issuer
    account: Account


@pytest.fixture
def world(session: Session) -> World:
    alice, bob = User(name="Alice", is_default=True), User(name="Bob")
    issuer = Issuer(name="Chase", slug="chase")
    session.add_all([alice, bob, issuer])
    session.flush()
    account = Account(
        user_id=alice.id,
        issuer_id=issuer.id,
        account_type="credit",
        display_name="Chase credit ...1234",
        mask="1234",
    )
    session.add(account)
    session.commit()
    return World(alice, bob, issuer, account)


def _statement(
    session: Session,
    sha: str,
    world: World,
    *,
    period_end: str | None = "2024-03-31",
    stored_path: str | None = None,
    status: str = "parsed",
    account: bool = True,
) -> Statement:
    row = Statement(
        file_sha256=sha,
        original_name="whatever 987654321.pdf",
        status=status,
        user_id=world.alice.id,
        account_id=world.account.id if account else None,
        period_end=period_end,
        stored_path=stored_path,
    )
    session.add(row)
    session.commit()
    return row


def _tax_doc(
    session: Session,
    sha: str,
    user: User,
    forms: list[tuple[str, str | None, bool]],
    *,
    year: int = 2024,
    stored_path: str | None = None,
) -> TaxDocument:
    row = TaxDocument(
        user_id=user.id,
        file_sha256=sha,
        original_name="w2.pdf",
        status="parsed",
        tax_year=year,
        stored_path=stored_path,
    )
    session.add(row)
    session.flush()
    for ordinal, (form_type, payer, corrected) in enumerate(forms):
        session.add(
            TaxForm(
                document_id=row.id,
                user_id=user.id,
                tax_year=year,
                form_type=form_type,
                role=FORM_ROLE[form_type],  # type: ignore[index]
                payer_name=payer,
                is_corrected=corrected,
                fingerprint=f"{sha}-{ordinal}-{user.id}",
            )
        )
    session.commit()
    return row


def _on_disk() -> list[str]:
    return sorted(rel for rel, _ in docs.iter_files(pdf_only=False))


# --- filing after an import ---------------------------------------------------------------------


def test_a_statement_is_filed_under_person_institution_and_month(
    session: Session, world: World, tmp_path: Path
) -> None:
    src, sha = _pdf(tmp_path)
    statement = _statement(session, sha, world)
    rel = doc_files.file_statement(session, statement, src)
    assert rel == "Alice/Statements/Chase/2024-03 Chase credit 1234.pdf"
    assert statement.stored_path == rel
    assert (documents_dir() / rel).read_bytes() == src.read_bytes()
    session.refresh(statement)
    assert statement.stored_path == rel  # committed
    assert "987654321" not in "".join(_on_disk())  # the upload's own name never reaches a path


def test_two_statements_of_one_month_get_numbers(
    session: Session, world: World, tmp_path: Path
) -> None:
    rels = []
    for _ in range(2):
        src, sha = _pdf(tmp_path)
        rels.append(doc_files.file_statement(session, _statement(session, sha, world), src))
    assert rels == [
        "Alice/Statements/Chase/2024-03 Chase credit 1234.pdf",
        "Alice/Statements/Chase/2024-03 Chase credit 1234 (2).pdf",
    ]


def test_an_unparsed_statement_goes_to_unsorted(
    session: Session, world: World, tmp_path: Path
) -> None:
    src, sha = _pdf(tmp_path)
    statement = _statement(session, sha, world, period_end=None, account=False, status="error")
    rel = doc_files.file_statement(session, statement, src)
    assert rel is not None and rel.startswith("Alice/Statements/Unsorted/")
    assert rel.endswith(f"Statement {sha[:8]}.pdf")


def test_a_statement_without_a_person_goes_to_shared(
    session: Session, world: World, tmp_path: Path
) -> None:
    src, sha = _pdf(tmp_path)
    statement = _statement(session, sha, world, account=False)
    statement.user_id = None
    session.commit()
    rel = doc_files.file_statement(session, statement, src)
    assert rel is not None and rel.startswith("Shared/Statements/")


def test_tax_documents_are_named_by_form_and_payer(
    session: Session, world: World, tmp_path: Path
) -> None:
    cases = [
        ([("w2", "Acme Corp", False)], "Alice/Tax/2024/W-2 - Acme Corp.pdf"),
        ([("1099-int", "Ally Bank", False)], "Alice/Tax/2024/1099-INT - Ally Bank.pdf"),
        ([("1040", None, False)], "Alice/Tax/2024/1040.pdf"),
        ([("w2", "Globex", True)], "Alice/Tax/2024/W-2 - Globex (corrected).pdf"),
    ]
    for forms, expected in cases:
        src, sha = _pdf(tmp_path)
        document = _tax_doc(session, sha, world.alice, forms)
        assert doc_files.file_tax_document(session, document, src) == expected
        assert document.stored_path == expected


def test_a_failed_copy_is_reported_not_raised(
    session: Session, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, sha = _pdf(tmp_path)
    statement = _statement(session, sha, world)

    def boom(*_a: object, **_k: object) -> str:
        raise OSError("read-only file system")

    monkeypatch.setattr(docs, "place_file", boom)
    assert doc_files.file_statement(session, statement, src) is None
    session.refresh(statement)
    assert statement.stored_path is None


# --- one file, several rows ---------------------------------------------------------------------


def test_the_same_pdf_for_two_rows_is_kept_once(
    session: Session, world: World, tmp_path: Path
) -> None:
    src, sha = _pdf(tmp_path)
    forms = [("w2", "Acme Corp", False)]
    first = _tax_doc(session, sha, world.alice, forms)
    second = _tax_doc(session, sha, world.bob, forms)
    a = doc_files.file_tax_document(session, first, src)
    b = doc_files.file_tax_document(session, second, src)
    assert a == b == "Alice/Tax/2024/W-2 - Acme Corp.pdf"
    assert _on_disk() == [a]  # one file for both rows


def test_deleting_one_row_keeps_a_file_another_row_uses(
    session: Session, world: World, tmp_path: Path
) -> None:
    src, sha = _pdf(tmp_path)
    forms = [("w2", "Acme Corp", False)]
    first = _tax_doc(session, sha, world.alice, forms)
    second = _tax_doc(session, sha, world.bob, forms)
    doc_files.file_tax_document(session, first, src)
    doc_files.file_tax_document(session, second, src)
    stored = first.stored_path

    session.delete(first)
    session.commit()
    doc_files.discard_copy(session, stored, sha)
    assert _on_disk() == [stored]  # the second row still needs it

    session.delete(second)
    session.commit()
    doc_files.discard_copy(session, second.stored_path, sha)
    assert _on_disk() == []
    assert not (documents_dir() / "Alice/Tax/2024").exists()  # the emptied year folder went
    assert (documents_dir() / "Alice/Tax").is_dir()  # the drop folders stay


def test_deleting_a_row_does_not_delete_a_file_the_user_moved(
    session: Session, world: World, tmp_path: Path
) -> None:
    src, sha = _pdf(tmp_path)
    statement = _statement(session, sha, world)
    rel = doc_files.file_statement(session, statement, src)
    assert rel is not None
    moved = documents_dir() / "Taxes 2024 folder" / "mine.pdf"
    moved.parent.mkdir()
    (documents_dir() / rel).rename(moved)
    session.delete(statement)
    session.commit()
    doc_files.discard_copy(session, rel, sha)
    assert moved.is_file()  # theirs now: only the file at the recorded path is ever deleted


def test_deleting_a_row_keeps_a_different_pdf_the_user_put_under_its_name(
    session: Session, world: World, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    src, sha = _pdf(tmp_path)
    statement = _statement(session, sha, world)
    rel = doc_files.file_statement(session, statement, src)
    assert rel is not None
    (documents_dir() / rel).write_bytes(b"%PDF- a different statement")  # same name, other content
    session.delete(statement)
    session.commit()
    with caplog.at_level("INFO"):
        doc_files.discard_copy(session, rel, sha)
    assert (documents_dir() / rel).read_bytes() == b"%PDF- a different statement"
    assert "not deleting" in caplog.text


def test_deleting_never_follows_a_foreign_absolute_path(
    session: Session, world: World, tmp_path: Path
) -> None:
    outside = tmp_path / "somewhere" / "precious.pdf"
    outside.parent.mkdir()
    outside.write_bytes(b"%PDF- precious")
    doc_files.discard_copy(session, str(outside), "9" * 64)
    assert outside.is_file()


def test_deleting_removes_the_upload_waiting_in_staging(
    session: Session, world: World, tmp_path: Path
) -> None:
    src, sha = _pdf(tmp_path)
    staging_dir().mkdir(parents=True, exist_ok=True)
    (staging_dir() / f"{sha}.pdf").write_bytes(src.read_bytes())
    doc_files.discard_copy(session, None, sha)
    assert not (staging_dir() / f"{sha}.pdf").exists()


# --- the old hash-named folder ------------------------------------------------------------------


def _legacy(
    session: Session, world: World, tmp_path: Path, *, kept: bool = True
) -> tuple[Statement, TaxDocument, str, str]:
    """A statement and a tax document whose PDFs sit in ``<home>/statements/<sha>.pdf``."""
    folder = legacy_statements_dir()
    folder.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    for _ in range(2):
        src, sha = _pdf(tmp_path)
        (folder / f"{sha}.pdf").write_bytes(src.read_bytes())
        out.append(sha)
    statement = _statement(
        session, out[0], world, stored_path=str(folder / f"{out[0]}.pdf") if kept else None
    )
    document = _tax_doc(
        session,
        out[1],
        world.alice,
        [("w2", "Acme Corp", False)],
        stored_path=str(folder / f"{out[1]}.pdf") if kept else None,
    )
    return statement, document, out[0], out[1]


def test_legacy_files_move_to_readable_names(
    session: Session, world: World, tmp_path: Path
) -> None:
    statement, document, _s_sha, _t_sha = _legacy(session, world, tmp_path)
    report = doc_files.migrate_legacy_files(session)
    session.refresh(statement)
    session.refresh(document)
    assert statement.stored_path == "Alice/Statements/Chase/2024-03 Chase credit 1234.pdf"
    assert document.stored_path == "Alice/Tax/2024/W-2 - Acme Corp.pdf"
    assert len(report.filed) == 2 and report.removed_folder
    assert not legacy_statements_dir().exists()  # an emptied statements/ is removed
    for row in (statement, document):
        assert hashlib.sha256((documents_dir() / row.stored_path).read_bytes()).hexdigest() == (
            row.file_sha256
        )


def test_the_carry_over_is_idempotent(session: Session, world: World, tmp_path: Path) -> None:
    _legacy(session, world, tmp_path)
    doc_files.migrate_legacy_files(session)
    before = _on_disk()
    again = doc_files.migrate_legacy_files(session)
    assert not again.changed and _on_disk() == before


def test_unconfirmed_uploads_go_back_to_staging(
    session: Session, world: World, tmp_path: Path
) -> None:
    _statement_row, _document, s_sha, _t = _legacy(session, world, tmp_path, kept=False)
    report = doc_files.migrate_legacy_files(session)
    assert report.staged == 2 and not report.filed
    assert (staging_dir() / f"{s_sha}.pdf").is_file()
    assert _on_disk() == []  # nothing is *kept* for a row that never kept a copy


def test_files_without_a_row_and_stray_files_are_left_alone(
    session: Session, world: World, tmp_path: Path
) -> None:
    _legacy(session, world, tmp_path)
    folder = legacy_statements_dir()
    orphan = folder / f"{'f' * 64}.pdf"
    orphan.write_bytes(b"%PDF- nobody's")
    partial = folder / "tmpabcd1234.pdf"
    partial.write_bytes(b"%PDF- half an upload")
    report = doc_files.migrate_legacy_files(session)
    assert orphan.is_file() and partial.is_file()  # never deleted, never moved
    assert report.unreferenced == [orphan.name] and report.other == [partial.name]
    assert not report.removed_folder and folder.is_dir()


def test_legacy_dry_run_changes_nothing(session: Session, world: World, tmp_path: Path) -> None:
    statement, _document, s_sha, _t = _legacy(session, world, tmp_path)
    report = doc_files.migrate_legacy_files(session, dry_run=True)
    assert len(report.filed) == 2
    assert (legacy_statements_dir() / f"{s_sha}.pdf").is_file()
    assert not documents_dir().exists() or _on_disk() == []
    session.refresh(statement)
    assert statement.stored_path is not None and Path(statement.stored_path).is_absolute()


def test_a_shared_legacy_file_is_filed_once(session: Session, world: World, tmp_path: Path) -> None:
    folder = legacy_statements_dir()
    folder.mkdir(parents=True)
    src, sha = _pdf(tmp_path)
    (folder / f"{sha}.pdf").write_bytes(src.read_bytes())
    stored = str(folder / f"{sha}.pdf")
    forms = [("w2", "Acme Corp", False)]
    a = _tax_doc(session, sha, world.alice, forms, stored_path=stored)
    b = _tax_doc(session, sha, world.bob, forms, stored_path=stored)
    doc_files.migrate_legacy_files(session)
    session.refresh(a)
    session.refresh(b)
    assert a.stored_path == b.stored_path == "Alice/Tax/2024/W-2 - Acme Corp.pdf"
    assert _on_disk() == [a.stored_path]


def test_a_crash_before_the_database_is_updated_leaves_a_consistent_state(
    session: Session, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement, document, s_sha, _t_sha = _legacy(session, world, tmp_path)
    real_place = docs.place_file
    calls = {"n": 0}

    def place_then_crash(*args: object, **kwargs: object) -> str:
        rel = real_place(*args, **kwargs)  # type: ignore[arg-type]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("power cut")  # after the copy, before the row is updated
        return rel

    monkeypatch.setattr(docs, "place_file", place_then_crash)
    with pytest.raises(RuntimeError):
        doc_files.migrate_legacy_files(session)
    session.rollback()
    # The old file is still there, the row still names it, and the copy that was already made is
    # harmless: no data lost, nothing half-updated.
    assert (legacy_statements_dir() / f"{s_sha}.pdf").is_file()
    session.refresh(statement)
    assert statement.stored_path is not None and Path(statement.stored_path).is_absolute()
    assert len(_on_disk()) == 1

    monkeypatch.setattr(docs, "place_file", real_place)
    report = doc_files.migrate_legacy_files(session)  # the next start finishes the job
    assert not report.errors
    session.refresh(statement)
    session.refresh(document)
    assert statement.stored_path == "Alice/Statements/Chase/2024-03 Chase credit 1234.pdf"
    assert len(_on_disk()) == 2  # no duplicate of the file copied before the crash
    assert not legacy_statements_dir().exists()


def test_a_crash_before_the_old_file_is_deleted_is_cleaned_up_next_time(
    session: Session, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement, _document, s_sha, _t = _legacy(session, world, tmp_path)
    old = legacy_statements_dir() / f"{s_sha}.pdf"
    real_unlink = Path.unlink

    def fail_for_old(self: Path, missing_ok: bool = False) -> None:
        if self == old:
            raise OSError("busy")
        real_unlink(self, missing_ok=missing_ok)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "unlink", fail_for_old)
        first = doc_files.migrate_legacy_files(session)
    assert first.errors and old.is_file()
    session.refresh(statement)
    assert statement.stored_path == "Alice/Statements/Chase/2024-03 Chase credit 1234.pdf"

    second = doc_files.migrate_legacy_files(session)
    assert second.duplicates_removed == 1 and not old.exists()
    assert len([p for p in _on_disk() if "Statements" in p]) == 1


def test_the_carry_over_never_overwrites_a_users_file(
    session: Session, world: World, tmp_path: Path
) -> None:
    _statement_row, _document, _s, _t = _legacy(session, world, tmp_path)
    taken = documents_dir() / "Alice/Statements/Chase/2024-03 Chase credit 1234.pdf"
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"%PDF- the user's own file")
    doc_files.migrate_legacy_files(session)
    assert taken.read_bytes() == b"%PDF- the user's own file"
    assert "Alice/Statements/Chase/2024-03 Chase credit 1234 (2).pdf" in _on_disk()


# --- the startup hook ---------------------------------------------------------------------------


def test_prepare_documents_creates_the_folder_and_carries_the_old_one_over(
    engine: Engine, session: Session, world: World, tmp_path: Path
) -> None:
    statement, _document, _s, _t = _legacy(session, world, tmp_path)
    doc_files.prepare_documents(engine)
    session.refresh(statement)
    assert statement.stored_path == "Alice/Statements/Chase/2024-03 Chase credit 1234.pdf"
    assert (documents_dir() / docs.README_NAME).is_file()
    assert not legacy_statements_dir().exists()


def test_prepare_documents_waits_for_the_schema(home: Path, tmp_path: Path) -> None:
    from alembic import command

    from personal_finance_analyzer.db.migrate import build_alembic_config
    from personal_finance_analyzer.db.session import make_engine_for_path

    db = home / "finance.db"
    command.upgrade(build_alembic_config(f"sqlite:///{db}"), "0010_user_merchant_pins_to_rules")
    folder = legacy_statements_dir()
    folder.mkdir(parents=True)
    (folder / f"{'a' * 64}.pdf").write_bytes(b"%PDF-")
    engine = make_engine_for_path(db)
    try:
        assert doc_files.migrate_legacy_documents(engine) is None  # not at head: nothing touched
        assert (folder / f"{'a' * 64}.pdf").is_file()
    finally:
        engine.dispose()


def test_prepare_documents_removes_an_empty_old_folder(engine: Engine, home: Path) -> None:
    legacy_statements_dir().mkdir(parents=True)
    doc_files.prepare_documents(engine)
    assert not legacy_statements_dir().exists()


def test_prepare_documents_survives_an_unwritable_folder(
    engine: Engine, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("PFA_DOCUMENTS_DIR", str(blocker / "docs"))
    doc_files.prepare_documents(engine)  # logs, does not raise


# --- reorganize ---------------------------------------------------------------------------------


def _filed(session: Session, world: World, tmp_path: Path) -> tuple[Statement, TaxDocument]:
    s_src, s_sha = _pdf(tmp_path)
    t_src, t_sha = _pdf(tmp_path)
    statement = _statement(session, s_sha, world)
    document = _tax_doc(session, t_sha, world.alice, [("w2", "Acme Corp", False)])
    doc_files.file_statement(session, statement, s_src)
    doc_files.file_tax_document(session, document, t_src)
    return statement, document


def test_reorganize_applies_a_renamed_person(
    session: Session, world: World, tmp_path: Path
) -> None:
    statement, document = _filed(session, world, tmp_path)
    old_paths = {statement.stored_path, document.stored_path}
    world.alice.name = "Alicia"
    session.commit()

    dry = doc_files.reorganize(session, dry_run=True)
    assert {c.old for c in dry.changes} == old_paths and dry.dry_run
    assert {c.new for c in dry.changes} == {
        "Alicia/Statements/Chase/2024-03 Chase credit 1234.pdf",
        "Alicia/Tax/2024/W-2 - Acme Corp.pdf",
    }
    session.refresh(statement)
    assert statement.stored_path in old_paths  # a dry run changes nothing
    assert set(_on_disk()) == old_paths

    real = doc_files.reorganize(session)
    session.refresh(statement)
    session.refresh(document)
    assert {c.new for c in real.changes} == {statement.stored_path, document.stored_path}
    assert set(_on_disk()) == {statement.stored_path, document.stored_path}
    assert not (documents_dir() / "Alice/Statements/Chase").exists()  # emptied folders go...
    assert (documents_dir() / "Alice/Statements").is_dir()  # ...but not the person's drop folders
    assert doc_files.reorganize(session).changes == []  # idempotent


def test_reorganize_adopts_a_file_the_user_moved_instead_of_moving_it_back(
    session: Session, world: World, tmp_path: Path
) -> None:
    statement, _document = _filed(session, world, tmp_path)
    assert statement.stored_path is not None
    mine = documents_dir() / "My Taxes" / "march.pdf"
    mine.parent.mkdir()
    (documents_dir() / statement.stored_path).rename(mine)
    report = doc_files.reorganize(session)
    session.refresh(statement)
    assert statement.stored_path == "My Taxes/march.pdf"
    assert mine.is_file()  # left exactly where they put it
    assert [c.action for c in report.changes if c.row_id == statement.id] == ["adopted"]


def test_reorganize_walks_the_folder_once_however_many_files_moved(
    session: Session, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement, document = _filed(session, world, tmp_path)
    for row in (statement, document):
        assert row.stored_path is not None
        moved = documents_dir() / "mine" / Path(row.stored_path).name
        moved.parent.mkdir(exist_ok=True)
        (documents_dir() / row.stored_path).rename(moved)
    walks: list[int] = []
    real = docs.iter_files

    def counting(*args, **kwargs):  # type: ignore[no-untyped-def]
        walks.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(docs, "iter_files", counting)
    report = doc_files.reorganize(session)
    assert [c.action for c in report.changes] == ["adopted", "adopted"]
    assert len(walks) == 1


def test_reorganize_reports_a_deleted_file_without_inventing_one(
    session: Session, world: World, tmp_path: Path
) -> None:
    statement, _document = _filed(session, world, tmp_path)
    assert statement.stored_path is not None
    (documents_dir() / statement.stored_path).unlink()
    report = doc_files.reorganize(session)
    assert report.missing == [("statement", statement.id)]
    session.refresh(statement)
    assert statement.stored_path is not None  # the row is not touched


def test_reorganize_avoids_collisions(session: Session, world: World, tmp_path: Path) -> None:
    statement, _document = _filed(session, world, tmp_path)
    other_src, other_sha = _pdf(tmp_path)
    other = _statement(session, other_sha, world, period_end="2024-04-30")
    doc_files.file_statement(session, other, other_src)
    other.period_end = "2024-03-31"  # now both belong in "2024-03 Chase credit 1234"
    session.commit()
    doc_files.reorganize(session)
    session.refresh(statement)
    session.refresh(other)
    assert sorted([statement.stored_path or "", other.stored_path or ""]) == [
        "Alice/Statements/Chase/2024-03 Chase credit 1234 (2).pdf",
        "Alice/Statements/Chase/2024-03 Chase credit 1234.pdf",
    ]


def test_reorganize_carries_the_old_folder_over_first(
    session: Session, world: World, tmp_path: Path
) -> None:
    statement, _document, _s, _t = _legacy(session, world, tmp_path)
    report = doc_files.reorganize(session)
    session.refresh(statement)
    assert report.legacy is not None and len(report.legacy.filed) == 2
    assert statement.stored_path == "Alice/Statements/Chase/2024-03 Chase credit 1234.pdf"


# --- check --------------------------------------------------------------------------------------


def test_check_lists_missing_moved_and_unreferenced_files(
    session: Session, world: World, tmp_path: Path
) -> None:
    statement, document = _filed(session, world, tmp_path)
    assert doc_files.check(session).ok and doc_files.check(session).orphans == []
    assert statement.stored_path and document.stored_path

    (documents_dir() / document.stored_path).unlink()  # deleted
    moved = documents_dir() / "elsewhere.pdf"
    (documents_dir() / statement.stored_path).rename(moved)  # moved
    stray = documents_dir() / "Alice" / "not mine.pdf"
    stray.parent.mkdir(exist_ok=True)
    stray.write_bytes(b"%PDF- something else")  # unreferenced
    (documents_dir() / "notes.txt").write_text("not a pdf", encoding="utf-8")

    report = doc_files.check(session)
    assert [m[:2] for m in report.missing] == [("tax document", document.id)]
    assert not report.ok
    assert [(m[0], m[1], m[3]) for m in report.moved] == [
        ("statement", statement.id, "elsewhere.pdf")
    ]
    assert report.orphans == [
        "Alice/not mine.pdf"
    ]  # moved files are not orphans; notes.txt is no PDF
    # Reading is all it did.
    assert stray.is_file() and moved.is_file()


def test_check_reports_leftover_temp_files(session: Session, world: World) -> None:
    folder = documents_dir() / "Alice"
    folder.mkdir(parents=True)
    (folder / ".abc.part").write_bytes(b"half")
    assert doc_files.check(session).leftovers == ["Alice/.abc.part"]


def test_check_counts_old_files_as_pending_not_missing(
    session: Session, world: World, tmp_path: Path
) -> None:
    _legacy(session, world, tmp_path)
    report = doc_files.check(session)
    assert report.ok and report.legacy_files == 2


# --- the "PDF not found" state --------------------------------------------------------------------


def test_pdf_missing_is_true_only_for_a_kept_copy_that_is_gone(
    session: Session, world: World, tmp_path: Path
) -> None:
    statement, _document = _filed(session, world, tmp_path)
    assert not doc_files.pdf_missing(statement)
    assert statement.stored_path is not None
    (documents_dir() / statement.stored_path).unlink()
    assert doc_files.pdf_missing(statement)
    statement.stored_path = None
    assert not doc_files.pdf_missing(statement)  # never kept: not "missing"


def test_every_row_still_resolves_after_filing(
    session: Session, world: World, tmp_path: Path
) -> None:
    _filed(session, world, tmp_path)
    for row in (*session.scalars(select(Statement)), *session.scalars(select(TaxDocument))):
        assert docs.kept_pdf(row.stored_path, row.file_sha256) is not None
