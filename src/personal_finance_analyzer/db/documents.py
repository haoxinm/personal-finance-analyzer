"""Which file belongs to which row: filing, re-naming, checking and removing the PDF copies.

`statements.stored_path` and `tax_documents.stored_path` hold the copy's path *relative to the
documents directory* (``Alex/Statements/Chase/2024-03 Chase credit 1234.pdf``); the content hash
(``file_sha256``) is the row's real identity, so a file the user moved or renamed is found again by
hash (`core.documents.kept_pdf`). This module is the database half of that design:

- `file_statement` / `file_tax_document`: after an import, give the PDF its readable name and
  record it (the first time, or again after the row's person/account/period changed);
- `discard_copy`: the file of a deleted row goes away unless another row still uses it;
- `migrate_legacy_files`: the one-time, idempotent, crash-safe move of the old
  ``<data dir>/statements/<sha256>.pdf`` files into the new layout (runs at startup);
- `reorganize`: re-apply the naming to every row on demand (after a person or account was
  renamed), adopting files the user moved by hand instead of dragging them back;
- `check`: what is missing, moved or not referenced.

One PDF (one hash) can be referenced by several rows (the same W-2 imported for two people; a
statement and a tax form are never the same file, but the code does not assume it): they share one
file, filed under the first row's person, and a file is deleted only when no row references it.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from personal_finance_analyzer.core import doc_names
from personal_finance_analyzer.core import documents as docs
from personal_finance_analyzer.core.documents import DocumentsError
from personal_finance_analyzer.core.paths import documents_dir, legacy_statements_dir, staging_dir
from personal_finance_analyzer.db.models import (
    Account,
    Issuer,
    Statement,
    TaxDocument,
    TaxForm,
    User,
    WatchedFile,
    utcnow_iso,
)

logger = logging.getLogger(__name__)

#: `watch_files.code` of a file whose row the user deleted in the app (the file was kept).
DELETED_IN_APP = "deleted_in_app"

_SHA_FILE = re.compile(r"^[0-9a-f]{64}\.pdf$")

Row = Statement | TaxDocument


def _root(root: Path | None) -> Path:
    return root if root is not None else documents_dir()


def _is_relative(stored_path: str) -> bool:
    return not Path(stored_path).is_absolute()


# --------------------------------------------------------------------------------------------
# What a row's file should be called
# --------------------------------------------------------------------------------------------


def _person_name(session: Session, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    user = session.get(User, user_id)
    return user.name if user is not None else None


def statement_target(session: Session, statement: Statement) -> tuple[str, str]:
    """``(folder, file stem)`` for a statement: its account's person, institution and label, and
    the month its period ends (`core.doc_names.statement_path`)."""
    account = session.get(Account, statement.account_id) if statement.account_id else None
    user_id = account.user_id if account is not None else statement.user_id
    issuer_id = account.issuer_id if account is not None else statement.issuer_id
    issuer = session.get(Issuer, issuer_id) if issuer_id is not None else None
    return doc_names.statement_path(
        person=_person_name(session, user_id),
        institution=issuer.name if issuer is not None else None,
        account_display_name=account.display_name if account is not None else None,
        period_end=statement.period_end,
        ingested_at=statement.ingested_at,
        sha256=statement.file_sha256,
    )


def tax_target(session: Session, document: TaxDocument) -> tuple[str, str]:
    """``(folder, file stem)`` for a tax document: its person, tax year, form types and payer."""
    forms = session.scalars(
        select(TaxForm).where(TaxForm.document_id == document.id).order_by(TaxForm.id)
    ).all()
    year = document.tax_year or next((f.tax_year for f in forms if f.tax_year), None)
    return doc_names.tax_path(
        person=_person_name(session, document.user_id),
        tax_year=year,
        forms=[(f.form_type, f.payer_name, f.is_corrected) for f in forms],
        ingested_at=document.ingested_at,
        sha256=document.file_sha256,
    )


def target_of(session: Session, row: Row) -> tuple[str, str]:
    if isinstance(row, Statement):
        return statement_target(session, row)
    return tax_target(session, row)


# --------------------------------------------------------------------------------------------
# Rows that share a file
# --------------------------------------------------------------------------------------------


def rows_with_sha(session: Session, sha256: str) -> list[Row]:
    """Every statement and tax document of this content hash (statements first, by id)."""
    statements = session.scalars(
        select(Statement).where(Statement.file_sha256 == sha256).order_by(Statement.id)
    ).all()
    documents = session.scalars(
        select(TaxDocument).where(TaxDocument.file_sha256 == sha256).order_by(TaxDocument.id)
    ).all()
    return [*statements, *documents]


def _all_rows(session: Session) -> list[Row]:
    statements = session.scalars(select(Statement).order_by(Statement.id)).all()
    documents = session.scalars(select(TaxDocument).order_by(TaxDocument.id)).all()
    return [*statements, *documents]


def _claims_checker(session: Session, own_sha: str) -> Callable[[str], bool]:
    """A predicate for "this relative path belongs to a row of another file", so a collision with
    a file the user deleted (its row still names the path) never hands that name to a stranger."""
    claimed: set[str] = set()
    for model in (Statement, TaxDocument):
        for stored, sha in session.execute(
            select(model.stored_path, model.file_sha256).where(model.stored_path.is_not(None))
        ):
            if stored and sha != own_sha and _is_relative(stored):
                claimed.add(stored.casefold())
    return lambda rel: rel.casefold() in claimed


def _sibling_copy(session: Session, row: Row, root: Path) -> str | None:
    """The relative path of a file another row of the same content already keeps (so the PDF is
    stored once), if that file exists."""
    for other in rows_with_sha(session, row.file_sha256):
        if other is row or not other.stored_path or not _is_relative(other.stored_path):
            continue
        path = docs.resolve(other.stored_path, root)
        if path is not None and path.is_file():
            return other.stored_path
    return None


def _set_path(session: Session, row: Row, rel: str, old: str | None = None) -> None:
    """Record ``rel`` for ``row`` and for every row that shared its file (``old`` is the path
    they shared before a rename)."""
    row.stored_path = rel
    if old:
        for other in rows_with_sha(session, row.file_sha256):
            if other.stored_path and other.stored_path.casefold() == old.casefold():
                other.stored_path = rel


# --------------------------------------------------------------------------------------------
# Filing after an import
# --------------------------------------------------------------------------------------------


def file_statement(
    session: Session, statement: Statement, source: Path | None = None, *, root: Path | None = None
) -> str | None:
    """Keep the PDF of ``statement`` under its readable name and record it; commits. ``source``
    is the file to copy from (default: the copy the row already has, else the upload in
    ``incoming/``). Returns the relative path, or ``None`` when there was nothing to file or the
    folder could not be written (logged; the row keeps working from ``incoming/``)."""
    return _file(session, statement, source, root)


def file_tax_document(
    session: Session, document: TaxDocument, source: Path | None = None, *, root: Path | None = None
) -> str | None:
    """`file_statement` for a tax document."""
    return _file(session, document, source, root)


def _file(session: Session, row: Row, source: Path | None, root: Path | None) -> str | None:
    base = _root(root)
    sha = row.file_sha256
    try:
        docs.ensure_documents_dir(base)
        folder, stem = target_of(session, row)
        old = row.stored_path
        current = docs.resolve(old, base) if old and _is_relative(old) else None
        if row.pdf_in_place and old and current is not None and current.is_file():
            # A file the user put there themselves stays exactly where and as they put it.
            return old
        claimed = _claims_checker(session, sha)
        in_place = False
        if current is not None and current.is_file():
            rel = docs.rename_within(current, folder, stem, sha, claimed=claimed, root=base)
        else:
            sibling = _sibling_copy(session, row, base)
            if sibling is not None:
                rel = sibling
                in_place = _path_in_place(session, sibling)
            else:
                src = source or docs.kept_pdf(old, sha, base) or docs.staged_pdf(sha)
                if src is None:
                    return None
                rel = docs.place_file(src, folder, stem, sha, claimed=claimed, root=base)
        _set_path(session, row, rel, old if current is not None else None)
        row.pdf_in_place = in_place  # (a file placed here is the app's own copy)
        session.commit()
    except (OSError, DocumentsError) as exc:
        session.rollback()
        logger.warning("could not keep the PDF copy of %s %s: %s", _kind(row), row.id, exc)
        return None
    return rel


def _kind(row: Row) -> str:
    return "statement" if isinstance(row, Statement) else "tax document"


def _path_in_place(session: Session, rel: str) -> bool:
    """Whether a row records ``rel`` as a file the user put there themselves."""
    return any(
        session.scalar(
            select(model.id).where(model.stored_path == rel, model.pdf_in_place.is_(True)).limit(1)
        )
        is not None
        for model in (Statement, TaxDocument)
    )


# --------------------------------------------------------------------------------------------
# Deleting
# --------------------------------------------------------------------------------------------


def _still_referenced(session: Session, sha256: str, stored_path: str | None) -> bool:
    for model in (Statement, TaxDocument):
        if session.scalar(
            select(model.id).where(model.file_sha256 == sha256, model.stored_path.is_not(None))
        ):
            return True
        if (
            stored_path
            and session.scalar(select(model.id).where(model.stored_path == stored_path).limit(1))
            is not None
        ):
            return True
    return False


def _any_row_with_sha(session: Session, sha256: str) -> bool:
    return any(
        session.scalar(select(model.id).where(model.file_sha256 == sha256).limit(1)) is not None
        for model in (Statement, TaxDocument)
    )


def discard_copy(
    session: Session, stored_path: str | None, sha256: str, *, root: Path | None = None
) -> None:
    """Remove the PDF of a row that has just been deleted (call after the delete is committed):
    the kept copy at its recorded path, unless another row still keeps that file, and the
    hash-named upload in ``incoming/`` when no row of this content is left. Only the file the row
    recorded is ever deleted: one the user moved elsewhere is theirs, and an absolute path from
    an old database is deleted only inside the data directory's own old ``statements/`` folder."""
    base = _root(root)
    if stored_path and not _still_referenced(session, sha256, stored_path):
        path = docs.resolve(stored_path, base)
        if path is not None and path.is_file() and _deletable(path, base):
            # The path is only a name: the user may have put another PDF under it. Delete the
            # file only if it is the content this row kept.
            if docs.file_sha256(path) == sha256:
                docs.discard_path(path, base)
            else:
                logger.info(
                    "not deleting %s: it is not the PDF the deleted row kept (the content "
                    "differs), so it is yours",
                    stored_path,
                )
    if not _any_row_with_sha(session, sha256):
        for folder in (staging_dir(), legacy_statements_dir()):
            (folder / f"{sha256}.pdf").unlink(missing_ok=True)


def remove_row_file(
    session: Session,
    stored_path: str | None,
    sha256: str,
    *,
    in_place: bool = False,
    delete_file: bool = False,
    root: Path | None = None,
) -> None:
    """What to do with the PDF of a row that has just been deleted (after the delete committed).

    The app's own copy goes (`discard_copy`). A file the user put in the documents folder
    themselves (``in_place``) is theirs: it stays, unless ``delete_file`` (the user ticked "also
    delete the PDF file"). A kept file is recorded as *ignored* by the folder scan (see
    `ignore_kept_file`), so the next scan does not import it again at once."""
    if in_place and not delete_file:
        discard_copy(session, None, sha256, root=root)  # only the staged upload, if any
        if stored_path:
            ignore_kept_file(session, stored_path, sha256, root=root)
        return
    discard_copy(session, stored_path, sha256, root=root)


def ignore_kept_file(
    session: Session, stored_path: str, sha256: str, *, root: Path | None = None
) -> None:
    """Record ``stored_path`` (a file the user's deleted row pointed at, left in place) in
    ``watch_files`` as ignored ("deleted in the app"), keyed by the file's size and mtime: the
    folder scan leaves it alone until the file changes or the user presses "Import again". Does
    nothing if the file is gone, is not a relative path, or another row still keeps it."""
    if not _is_relative(stored_path) or _still_referenced(session, sha256, stored_path):
        return
    path = docs.resolve(stored_path, _root(root))
    if path is None:
        return
    try:
        stat = path.stat()
    except OSError:
        return
    record = session.scalar(select(WatchedFile).where(WatchedFile.path == stored_path))
    now = utcnow_iso()
    if record is None:
        record = WatchedFile(path=stored_path, first_seen=now)
        session.add(record)
    record.sha256 = sha256
    record.size_bytes = stat.st_size
    record.mtime_ns = stat.st_mtime_ns
    record.outcome = "ignored"
    record.code = DELETED_IN_APP
    record.message = "You deleted this document in the app; the PDF file was left where it is."
    record.hint = (
        "It is not imported again unless the file changes. To import it again, press "
        "Import again; to get rid of it, delete the file."
    )
    record.kind = None
    record.statement_id = None
    record.tax_document_id = None
    record.warnings = None
    record.last_seen = now
    session.commit()


def _deletable(path: Path, root: Path) -> bool:
    if root in path.parents:
        return True
    return legacy_statements_dir() in path.parents


# --------------------------------------------------------------------------------------------
# The one-time upgrade: <data dir>/statements/<sha>.pdf -> readable names
# --------------------------------------------------------------------------------------------


@dataclass
class LegacyReport:
    """What `migrate_legacy_files` did (or, in a dry run, would do)."""

    #: ``(legacy file name, relative path)`` of files filed under a readable name.
    filed: list[tuple[str, str]] = field(default_factory=list)
    #: Files not kept by any row (unconfirmed uploads) moved to ``incoming/``.
    staged: int = 0
    #: Legacy files whose content was already filed (an earlier, interrupted run): removed.
    duplicates_removed: int = 0
    #: Legacy files no row references: left where they are.
    unreferenced: list[str] = field(default_factory=list)
    #: Entries that are not ``<sha256>.pdf`` (partial uploads...): left where they are.
    other: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    removed_folder: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.filed or self.staged or self.duplicates_removed)


def legacy_files_pending(legacy_dir: Path | None = None) -> bool:
    """Whether the old ``statements/`` folder holds any ``<sha256>.pdf`` (cheap: one listing)."""
    folder = legacy_dir if legacy_dir is not None else legacy_statements_dir()
    try:
        return any(_SHA_FILE.fullmatch(p.name) for p in folder.iterdir())
    except OSError:
        return False


def migrate_legacy_files(
    session: Session,
    *,
    legacy_dir: Path | None = None,
    root: Path | None = None,
    staging: Path | None = None,
    dry_run: bool = False,
) -> LegacyReport:
    """Move the old hash-named PDFs into the readable layout.

    For every ``<legacy_dir>/<sha256>.pdf``: if a row that *kept* it (``stored_path`` set) refers
    to it, copy it to its readable name under ``root`` (fsynced, never overwriting), record the
    new relative path in one transaction, and only then delete the old file; if only unkept rows
    (an upload still waiting for its extractor) use it, move it to ``incoming/``; if no row
    refers to it, leave it alone. A crash at any point leaves a consistent state, and running it
    again finishes the job without duplicating anything (a copy that already exists with the same
    content is reused). The old folder is removed once it is empty.

    The database must be at the current schema (callers check). ``legacy_dir``/``root``/``staging``
    default to the configured locations; a restore passes its staging directories instead.
    """
    legacy = legacy_dir if legacy_dir is not None else legacy_statements_dir()
    base = _root(root)
    stage = staging if staging is not None else staging_dir()
    report = LegacyReport()
    if not legacy.is_dir():
        return report
    if root is None and not dry_run:
        docs.ensure_documents_dir(base)

    files: dict[str, Path] = {}
    try:
        entries = sorted(legacy.iterdir())
    except OSError as exc:
        report.errors.append(f"cannot list {legacy}: {exc}")
        return report
    for entry in entries:
        if _SHA_FILE.fullmatch(entry.name) and entry.is_file() and not entry.is_symlink():
            files[entry.name[:-4]] = entry
        else:
            report.other.append(entry.name)

    order: dict[str, tuple[int, int]] = {}
    for row in _all_rows(session):
        order.setdefault(row.file_sha256, (0 if isinstance(row, Statement) else 1, row.id))
    for sha in sorted(files, key=lambda s: order.get(s, (2, 0))):
        src = files[sha]
        rows = rows_with_sha(session, sha)
        if not rows:
            report.unreferenced.append(src.name)
            continue
        try:
            _migrate_one(session, src, sha, rows, base, stage, report, dry_run=dry_run)
        except (OSError, DocumentsError) as exc:
            session.rollback()
            report.errors.append(f"{src.name}: {exc}")
            logger.warning("could not move %s into the documents folder: %s", src.name, exc)

    if not dry_run:
        try:
            legacy.rmdir()  # only succeeds when empty: never removes anything else
            report.removed_folder = True
        except OSError:
            pass
    if report.changed and not dry_run:
        logger.info(
            "moved %d PDF(s) into %s (%d back to staging, %d duplicates removed)",
            len(report.filed),
            base,
            report.staged,
            report.duplicates_removed,
        )
    return report


def _migrate_one(
    session: Session,
    src: Path,
    sha: str,
    rows: Sequence[Row],
    base: Path,
    stage: Path,
    report: LegacyReport,
    *,
    dry_run: bool,
) -> None:
    kept = [r for r in rows if r.stored_path]
    if not kept:  # an upload waiting for its extractor (or a leftover): back to staging
        if dry_run:
            report.staged += 1
            return
        dest = stage / src.name
        stage.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if docs.file_sha256(dest) != sha:
                report.errors.append(f"{src.name}: a different file is already in {stage}")
                return
            src.unlink()
            report.duplicates_removed += 1
            return
        docs.publish_no_clobber_rename(src, dest)
        report.staged += 1
        return

    # Already carried over by an interrupted run? Then the legacy file is a verified duplicate.
    for row in kept:
        assert row.stored_path is not None
        done = docs.resolve(row.stored_path, base) if _is_relative(row.stored_path) else None
        if done is not None and done.is_file() and docs.file_sha256(done) == sha:
            if dry_run:
                report.duplicates_removed += 1
                return
            for other in kept:
                other.stored_path = row.stored_path
            session.commit()
            src.unlink()
            report.duplicates_removed += 1
            return

    folder, stem = target_of(session, kept[0])
    if dry_run:
        report.filed.append((src.name, f"{folder}/{doc_names.file_name(stem)}"))
        return
    rel = docs.place_file(src, folder, stem, sha, claimed=_claims_checker(session, sha), root=base)
    for row in kept:
        row.stored_path = rel
    session.commit()  # the new path is recorded before the old file is deleted
    src.unlink()
    report.filed.append((src.name, rel))


def migrate_legacy_documents(engine: Engine) -> LegacyReport | None:
    """The startup step: carry ``<data dir>/statements/`` over into the documents folder if it is
    still there and the database is at the current schema (otherwise `pfa migrate` has not run
    yet and nothing is touched). Never raises: a problem is logged and retried at the next start.
    Returns the report, or ``None`` when there was nothing to do."""
    from personal_finance_analyzer.db.migrate import is_at_head

    if not legacy_files_pending():
        _remove_empty_legacy_dir()
        return None
    try:
        if not is_at_head(engine):
            return None
        with Session(engine) as session:
            return migrate_legacy_files(session)
    except Exception as exc:
        logger.warning("could not move the PDF copies into the documents folder yet: %s", exc)
        return None


def prepare_documents(engine: Engine) -> None:
    """Startup step of every command that opens the database and of the web server: create the
    documents folder (with its README.txt if absent) and carry the old ``statements/`` folder over
    into it. Never raises, and does nothing to a folder it cannot write (logged instead)."""
    try:
        docs.ensure_documents_dir()
    except OSError as exc:
        logger.warning("cannot use the documents folder %s: %s", documents_dir(), exc)
        return
    migrate_legacy_documents(engine)


def _remove_empty_legacy_dir() -> None:
    with contextlib.suppress(OSError):  # only succeeds when empty
        legacy_statements_dir().rmdir()


# --------------------------------------------------------------------------------------------
# reorganize
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    kind: str  # statement | tax document
    row_id: int
    old: str
    new: str
    action: str  # renamed | adopted


@dataclass
class ReorganizeReport:
    changes: list[Change] = field(default_factory=list)
    unchanged: int = 0
    #: Rows whose file could not be found by path or content: nothing to rename.
    missing: list[tuple[str, int]] = field(default_factory=list)
    legacy: LegacyReport | None = None
    dry_run: bool = False


def reorganize(
    session: Session, *, dry_run: bool = False, root: Path | None = None
) -> ReorganizeReport:
    """Give every kept PDF the name the current data calls for (after a person or account was
    renamed, a statement moved to another account...).

    A file at its recorded path is renamed (and its emptied folders removed). A file that is no
    longer there but is found by content elsewhere in the folder was moved by the user on purpose:
    it is *adopted* (its new place is recorded) and left where it is. Rows sharing one file keep
    sharing it. ``dry_run`` reports what would change without touching anything. The old
    ``statements/`` folder is carried over first (only reported in a dry run)."""
    base = _root(root)
    report = ReorganizeReport(dry_run=dry_run)
    if legacy_files_pending():
        report.legacy = migrate_legacy_files(session, root=base, dry_run=dry_run)

    groups: dict[str, list[Row]] = {}
    for row in _all_rows(session):
        if row.stored_path and not row.pdf_in_place:  # a file the user put there stays put
            groups.setdefault(row.file_sha256, []).append(row)
    planned: set[str] = set()
    index = docs.ShaIndex(base)  # one walk for the whole run, built only if a file is missing
    for sha, rows in sorted(groups.items(), key=lambda kv: _order_key(kv[1][0])):
        _reorganize_group(session, sha, rows, base, report, planned, index, dry_run=dry_run)
    if not dry_run:
        session.commit()
    return report


def _order_key(row: Row) -> tuple[int, int]:
    return (0 if isinstance(row, Statement) else 1, row.id)


def _reorganize_group(
    session: Session,
    sha: str,
    rows: list[Row],
    base: Path,
    report: ReorganizeReport,
    planned: set[str],
    index: docs.ShaIndex,
    *,
    dry_run: bool,
) -> None:
    first = rows[0]
    old = first.stored_path
    assert old is not None
    current: Path | None = None
    for row in rows:
        assert row.stored_path is not None
        candidate = docs.resolve(row.stored_path, base) if _is_relative(row.stored_path) else None
        if candidate is not None and candidate.is_file():
            current, old = candidate, row.stored_path
            break
    if current is None:
        found = index.find(sha)
        if found is None:
            report.missing.append((_kind(first), first.id))
            return
        new = PurePosixPath(*found.relative_to(base).parts).as_posix()
        report.changes.append(Change(_kind(first), first.id, old, new, "adopted"))
        if not dry_run:
            for row in rows:
                row.stored_path = new
        return

    folder, stem = target_of(session, first)
    claimed = _claims_checker(session, sha)
    if dry_run:
        new = _virtual_name(base, folder, stem, sha, current, claimed, planned)
        planned.add(new.casefold())
    else:
        new = docs.rename_within(current, folder, stem, sha, claimed=claimed, root=base)
    if new == old and all(r.stored_path == old for r in rows):
        report.unchanged += 1
        return
    if new != old:
        report.changes.append(Change(_kind(first), first.id, old, new, "renamed"))
    if not dry_run:
        for row in rows:
            row.stored_path = new
        session.flush()
        docs.prune_empty_dirs(current.parent, base)
        session.commit()


def _virtual_name(
    base: Path,
    folder: str,
    stem: str,
    sha: str,
    current: Path,
    claimed: Callable[[str], bool],
    planned: set[str],
) -> str:
    """The relative path `rename_within` would pick, without touching the disk."""
    directory = base.joinpath(*PurePosixPath(folder).parts)
    try:
        listing = {p.name.casefold(): p.name for p in directory.iterdir()}
    except OSError:
        listing = {}
    for attempt in itertools.count(1):
        name = doc_names.file_name(stem, doc_names.collision_suffix(attempt))
        rel = f"{folder}/{name}"
        existing = listing.get(name.casefold())
        if existing is not None:
            other = directory / existing
            try:
                same = other.samefile(current)
            except OSError:
                same = False
            if same or docs.file_sha256(other) == sha:
                return f"{folder}/{existing}"
            continue
        if rel.casefold() in planned or claimed(rel):
            continue
        return rel
    raise AssertionError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------------------------


@dataclass
class CheckReport:
    #: ``(kind, row id, recorded path)`` of rows whose PDF is nowhere to be found.
    missing: list[tuple[str, int, str]] = field(default_factory=list)
    #: ``(kind, row id, recorded path, where it is now)``: found by content, not at its path.
    moved: list[tuple[str, int, str, str]] = field(default_factory=list)
    #: Relative paths of PDFs in the folder that no row refers to.
    orphans: list[str] = field(default_factory=list)
    #: Hash-named files still in the old ``statements/`` folder.
    legacy_files: int = 0
    #: Leftover temporary files/folders from an interrupted copy or restore.
    leftovers: list[str] = field(default_factory=list)
    documents_dir: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.missing


def check(session: Session, *, root: Path | None = None) -> CheckReport:
    """Compare the rows with the folder (read-only): PDFs that are missing, found elsewhere by
    content, or not referenced by any row. Hashes only files that no row names by path."""
    base = _root(root)
    report = CheckReport(documents_dir=base)
    on_disk = dict(docs.iter_files(base))
    referenced: set[str] = set()
    lookups: list[Row] = []
    for row in _all_rows(session):
        if not row.stored_path:
            continue
        path = docs.resolve(row.stored_path, base) if _is_relative(row.stored_path) else None
        if path is not None and path.is_file():
            if base in path.parents:
                referenced.add(PurePosixPath(*path.relative_to(base).parts).as_posix())
            continue
        lookups.append(row)
    if lookups:
        by_sha: dict[str, str] = {}
        for rel, path in on_disk.items():
            if rel in referenced:
                continue
            digest = docs.file_sha256(path)
            if digest is not None:
                by_sha.setdefault(digest, rel)
        for row in lookups:
            assert row.stored_path is not None
            found = by_sha.get(row.file_sha256)
            if found is not None:
                referenced.add(found)
                report.moved.append((_kind(row), row.id, row.stored_path, found))
            elif _legacy_copy(row.file_sha256):
                continue  # still in the old folder: the upgrade will carry it over
            else:
                report.missing.append((_kind(row), row.id, row.stored_path))
    report.orphans = sorted(rel for rel in on_disk if rel not in referenced)
    report.legacy_files = sum(
        1 for p in _legacy_listing() if _SHA_FILE.fullmatch(p.name) and p.is_file()
    )
    report.leftovers = _leftovers(base)
    return report


def _legacy_copy(sha: str) -> bool:
    return (legacy_statements_dir() / f"{sha}.pdf").is_file()


def _legacy_listing() -> list[Path]:
    try:
        return list(legacy_statements_dir().iterdir())
    except OSError:
        return []


def _leftovers(base: Path) -> list[str]:
    found: list[str] = []
    try:
        for entry in base.rglob(".*"):
            if entry.name.endswith(".part") or entry.name.startswith(".pfa-restore-"):
                found.append(PurePosixPath(*entry.relative_to(base).parts).as_posix())
    except OSError:
        pass
    return sorted(found)


def pdf_missing(row: Row) -> bool:
    """Whether ``row`` recorded a kept copy that cannot be found any more (not at its path, not
    anywhere in the folder by content): the UI's "PDF not found" state."""
    return bool(row.stored_path) and docs.kept_pdf(row.stored_path, row.file_sha256) is None
