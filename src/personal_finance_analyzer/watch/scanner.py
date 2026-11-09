"""One scan of the documents folder: find new PDFs, wait until they are stable, import them.

The steps of `FolderScanner.scan`:

1. List every visible PDF under the documents folder (`core.documents.iter_files`; hidden entries,
   symlinks, ``~$`` lock files, the staging folder and the README are skipped).
2. Skip, without reading them, the files the app already knows: those named by a statement's or tax
   document's ``stored_path`` (which includes everything the app filed itself) and those whose
   ``watch_files`` record has the same size and modification time (a failure is not retried until
   the file changes or the user presses Retry).
3. A new file waits until it is *stable*: the same size and mtime on two consecutive scans (or, for
   a manual scan, across a short settle pause), and it looks like a complete PDF (``%PDF-`` first,
   ``%%EOF`` near the end). A file still unfinished after `INCOMPLETE_AFTER_S` is reported as
   failed instead of waiting for ever.
4. A stable file is attributed by its folder (`classify`), looked up by content hash (the hash is
   cached by path, size and mtime, so nothing is read twice) and imported through the same
   pipelines as an upload: statements through `propose_import` + `confirm_import` (+ classification
   when the proposal is confident), tax forms through `import_tax_document`. The file is
   recorded as the row's PDF *where it is*; nothing is copied, renamed or moved.
5. Every outcome is recorded in `watch_files`.

A scan holds a cross-process file lock for its duration (`core.filelock`): two servers on one data
directory, or a server and `pfa documents scan`, never import the same files at the same time.
Everything here is synchronous; the server runs it on a worker thread with its own sessions.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from personal_finance_analyzer.core import documents as docs
from personal_finance_analyzer.core.filelock import FileLock
from personal_finance_analyzer.core.paths import documents_dir, get_home, staging_dir
from personal_finance_analyzer.db.models import (
    Statement,
    TaxDocument,
    User,
    WatchedFile,
    utcnow_iso,
)
from personal_finance_analyzer.watch.classify import PATH_CODES, PathClass, classify_path

logger = logging.getLogger(__name__)

#: A file whose size and mtime stopped changing but that still does not look like a complete PDF
#: is reported as failed once it is this old (seconds).
INCOMPLETE_AFTER_S = 10 * 60
#: Successful records kept for the "recent imports" list.
KEEP_SUCCESSES = 500
#: How many bytes at the end of a file are searched for ``%%EOF``.
_EOF_WINDOW = 2048
#: The pause of a manual scan (`pfa documents scan`, "Scan now") that stands in for waiting for a
#: second scan: a file is stable when it is unchanged after it.
MANUAL_SETTLE_S = 2.0

LOCK_NAME = "watch.lock"

SUCCESS_OUTCOMES = ("imported", "known")


def lock_path() -> Path:
    return get_home() / LOCK_NAME


@dataclass
class ScanItem:
    """What a scan did (or, in a dry run, would do) with one file."""

    path: str
    #: imported | known | failed | ignored | waiting | would_import
    action: str
    kind: str | None = None
    person: str | None = None
    message: str | None = None
    code: str | None = None


@dataclass
class ScanReport:
    started_at: str = field(default_factory=utcnow_iso)
    finished_at: str | None = None
    dry_run: bool = False
    #: Another scan holds the lock: nothing was done.
    busy: bool = False
    error: str | None = None
    #: PDFs seen in the folder (including those already known).
    scanned: int = 0
    items: list[ScanItem] = field(default_factory=list)

    def count(self, action: str) -> int:
        return sum(1 for i in self.items if i.action == action)

    @property
    def imported(self) -> int:
        return self.count("imported")

    @property
    def known(self) -> int:
        return self.count("known")

    @property
    def failed(self) -> int:
        return self.count("failed")

    @property
    def ignored(self) -> int:
        return self.count("ignored")

    @property
    def waiting(self) -> int:
        return self.count("waiting")

    @property
    def would_import(self) -> int:
        return self.count("would_import")


@dataclass
class _Candidate:
    rel: str
    path: Path
    size: int
    mtime_ns: int


@dataclass
class _Outcome:
    outcome: str  # imported | known | failed | ignored
    code: str | None = None
    message: str | None = None
    hint: str | None = None
    statement_id: int | None = None
    tax_document_id: int | None = None
    warnings: list[str] = field(default_factory=list)


def has_pdf_shape(path: Path, size: int) -> bool:
    """``%PDF-`` at the start and ``%%EOF`` near the end: the file is a whole PDF, not one that is
    still being copied."""
    try:
        with path.open("rb") as fh:
            if fh.read(5) != b"%PDF-":
                return False
            fh.seek(max(0, size - _EOF_WINDOW))
            return b"%%EOF" in fh.read(_EOF_WINDOW)
    except OSError:
        return False


def _unreadable(path: Path) -> bool:
    """Whether the file cannot be opened at all (typically: owned by another user, no read access):
    waiting will not fix that, so it is reported at once."""
    try:
        with path.open("rb"):
            return False
    except OSError:
        return True


def _skip_name(rel: str, staging: Path | None, root: Path) -> bool:
    parts = PurePosixPath(rel).parts
    if any(p.startswith("~$") for p in parts):
        return True
    if staging is not None and len(parts) > 1:
        try:
            return (root / parts[0]).resolve() == staging
        except OSError:
            return False
    return False


class FolderScanner:
    """Scans the documents folder. One instance keeps what the previous scan saw (for the
    "unchanged across two scans" rule), so the server uses one for its lifetime; a command makes a
    new one and uses the settle pause instead."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        root: Path | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._root = root
        self._clock = clock
        self._sleep = sleep
        #: path -> (size, mtime_ns) of files that still wait for their first import, as of the
        #: previous scan.
        self._pending: dict[str, tuple[int, int]] = {}

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else documents_dir()

    @property
    def waiting_paths(self) -> int:
        return len(self._pending)

    # ------------------------------------------------------------------ the scan

    def scan(self, *, dry_run: bool = False, settle_seconds: float = 0.0) -> ScanReport:
        """Run one scan. ``settle_seconds`` > 0 (manual scans): files seen for the first time are
        re-checked after that pause and count as stable if they did not change. ``dry_run`` reports
        what would happen and writes nothing (no import, no record)."""
        report = ScanReport(dry_run=dry_run)
        lock = FileLock(lock_path())
        try:
            if not lock.try_acquire():
                report.busy = True
                return report
        except OSError as exc:
            report.error = f"cannot take the scan lock: {exc.strerror or exc}"
            return report
        try:
            self._scan_locked(report, dry_run=dry_run, settle_seconds=settle_seconds)
        except Exception as exc:  # a scan must never take the server down
            logger.warning("folder scan failed: %s", type(exc).__name__, exc_info=True)
            report.error = f"The scan failed ({type(exc).__name__}); see the log."
        finally:
            lock.release()
            report.finished_at = utcnow_iso()
        return report

    def _scan_locked(self, report: ScanReport, *, dry_run: bool, settle_seconds: float) -> None:
        root = self.root
        if not root.is_dir():
            report.error = f"The documents folder {root} does not exist."
            return
        with self._session_factory() as session:
            people = [(u.id, u.name) for u in session.scalars(select(User).order_by(User.id))]
            known_paths = _stored_paths(session)
            records = {r.path: r for r in session.scalars(select(WatchedFile))}
            if not dry_run:
                self._settle_records(session, records)
            staging = _resolved(staging_dir())

            seen: set[str] = set()
            candidates: list[_Candidate] = []
            for rel, path in docs.iter_files(root):
                if _skip_name(rel, staging, root):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                report.scanned += 1
                seen.add(rel)
                if rel.casefold() in known_paths:
                    continue
                record = records.get(rel)
                if (
                    record is not None
                    and record.size_bytes == stat.st_size
                    and record.mtime_ns == stat.st_mtime_ns
                    and not _stale(record, people)
                ):
                    if record.outcome in ("failed", "ignored") and not dry_run:
                        record.last_seen = utcnow_iso()
                    continue
                candidates.append(_Candidate(rel, path, stat.st_size, stat.st_mtime_ns))

            ready = self._stable(candidates, report, settle_seconds)
            for candidate in ready:
                item = self._handle(session, candidate, people, report, dry_run=dry_run)
                if item is not None:
                    report.items.append(item)

            if not dry_run:
                self._cleanup(session, seen)
                session.commit()

    # ------------------------------------------------------------------ stability

    def _stable(
        self, candidates: list[_Candidate], report: ScanReport, settle_seconds: float
    ) -> list[_Candidate]:
        """The candidates that are unchanged since the last scan (or across the settle pause) and
        look like whole PDFs; the rest are reported as waiting."""
        previous = self._pending
        current = {c.rel: (c.size, c.mtime_ns) for c in candidates}
        stable = [c for c in candidates if previous.get(c.rel) == current[c.rel]]
        fresh = [c for c in candidates if c not in stable]
        if fresh and settle_seconds > 0:
            self._sleep(settle_seconds)
            for candidate in fresh:
                try:
                    stat = candidate.path.stat()
                except OSError:
                    continue
                if (stat.st_size, stat.st_mtime_ns) == (candidate.size, candidate.mtime_ns):
                    stable.append(candidate)
        stable_rels = {c.rel for c in stable}
        self._pending = {rel: key for rel, key in current.items() if rel not in stable_rels}
        # Files that are stable but not (yet) whole PDFs stay pending with their key, so the next
        # scan still finds them unchanged.
        ready: list[_Candidate] = []
        now = self._clock()
        for candidate in sorted(stable, key=lambda c: c.rel):
            if has_pdf_shape(candidate.path, candidate.size) or _unreadable(candidate.path):
                ready.append(candidate)  # (an unreadable file is reported by `_handle` at once)
                continue
            self._pending[candidate.rel] = (candidate.size, candidate.mtime_ns)
            age = now - candidate.mtime_ns / 1e9
            if age >= INCOMPLETE_AFTER_S:
                ready.append(candidate)  # handled as a failure by `_handle`
            else:
                report.items.append(
                    ScanItem(
                        candidate.rel,
                        "waiting",
                        message="Not a complete PDF yet (still being copied?).",
                        code="incomplete_pdf",
                    )
                )
        for candidate in candidates:
            if candidate.rel in self._pending and candidate not in stable:
                report.items.append(
                    ScanItem(
                        candidate.rel,
                        "waiting",
                        message="New file: waiting to see that it stops changing.",
                        code="settling",
                    )
                )
        return ready

    # ------------------------------------------------------------------ one file

    def _handle(
        self,
        session: Session,
        candidate: _Candidate,
        people: list[tuple[int, str]],
        report: ScanReport,
        *,
        dry_run: bool,
    ) -> ScanItem | None:
        parts = PurePosixPath(candidate.rel).parts
        cls = classify_path(parts, people)
        self._pending.pop(candidate.rel, None)
        try:
            if _unreadable(candidate.path):
                outcome = _Outcome(
                    "failed",
                    code="unreadable",
                    message="The app is not allowed to read this file.",
                    hint=(
                        "Give the user the app runs as (PUID/PGID in Docker) read access to the "
                        "file, then press Retry."
                    ),
                )
            elif not has_pdf_shape(candidate.path, candidate.size):
                outcome = _Outcome(
                    "failed",
                    code="incomplete_pdf",
                    message="This file is not a complete PDF (it may be damaged or not a PDF).",
                    hint="Copy the file again, then press Retry.",
                )
            elif cls.status == "unrecognised":
                outcome = _Outcome(
                    "ignored", code=cls.code, message=cls.message, hint=cls.hint or None
                )
            elif cls.status != "ok":
                outcome = _Outcome(
                    "failed", code=cls.code, message=cls.message, hint=cls.hint or None
                )
            else:
                outcome = self._import(session, candidate, cls, dry_run=dry_run)
        except Exception as exc:
            session.rollback()
            logger.warning("folder import failed: %s", type(exc).__name__)
            outcome = _Outcome(
                "failed",
                code="error",
                message=f"Unexpected error ({type(exc).__name__}); see the log.",
            )
        if outcome.outcome == "would_import":
            return ScanItem(
                candidate.rel,
                "would_import",
                kind=cls.kind,
                person=cls.person_name,
                message=outcome.message,
            )
        if dry_run:
            return ScanItem(
                candidate.rel,
                outcome.outcome,
                kind=cls.kind,
                person=cls.person_name or cls.person_folder,
                message=outcome.message,
                code=outcome.code,
            )
        self._record(session, candidate, cls, outcome)
        return ScanItem(
            candidate.rel,
            outcome.outcome,
            kind=cls.kind,
            person=cls.person_name or cls.person_folder,
            message=outcome.message,
            code=outcome.code,
        )

    def _import(
        self, session: Session, candidate: _Candidate, cls: PathClass, *, dry_run: bool
    ) -> _Outcome:
        from personal_finance_analyzer.watch import importers

        sha = docs.file_sha256(candidate.path)
        if sha is None:
            return _Outcome("failed", code="unreadable", message="The file could not be read.")
        assert cls.user_id is not None and cls.kind is not None
        ctx = importers.Context(
            session=session,
            session_factory=self._session_factory,
            path=candidate.path,
            rel=candidate.rel,
            sha=sha,
            user_id=cls.user_id,
            person_name=cls.person_name or "",
            folder_year=cls.folder_year,
            dry_run=dry_run,
        )
        if cls.kind == "statement":
            result = importers.import_statement(ctx)
        else:
            result = importers.import_tax(ctx)
        return _Outcome(
            result.outcome,
            code=result.code,
            message=result.message,
            hint=result.hint,
            statement_id=result.statement_id,
            tax_document_id=result.tax_document_id,
            warnings=result.warnings,
        )

    # ------------------------------------------------------------------ records

    def _record(
        self, session: Session, candidate: _Candidate, cls: PathClass, outcome: _Outcome
    ) -> None:
        record = session.scalar(select(WatchedFile).where(WatchedFile.path == candidate.rel))
        now = utcnow_iso()
        if record is None:
            record = WatchedFile(path=candidate.rel, first_seen=now)
            session.add(record)
        record.sha256 = docs.file_sha256(candidate.path)
        record.size_bytes = candidate.size
        record.mtime_ns = candidate.mtime_ns
        record.outcome = outcome.outcome
        record.code = outcome.code
        record.message = outcome.message
        record.hint = outcome.hint
        record.kind = cls.kind
        record.folder_person = cls.person_folder
        record.user_id = cls.user_id
        record.statement_id = outcome.statement_id
        record.tax_document_id = outcome.tax_document_id
        record.warnings = json.dumps(outcome.warnings) if outcome.warnings else None
        record.last_seen = now
        session.commit()

    def _settle_records(self, session: Session, records: dict[str, WatchedFile]) -> None:
        """A failed statement the user later finished by hand (the Import page) is a success."""
        linked = {
            r.statement_id: r for r in records.values() if r.outcome == "failed" and r.statement_id
        }
        if not linked:
            return
        parsed = session.scalars(
            select(Statement.id).where(Statement.id.in_(list(linked)), Statement.status == "parsed")
        ).all()
        for statement_id in parsed:
            record = linked[statement_id]
            record.outcome = "imported"
            record.code = None
            record.message = "Imported later from the Import page."
            record.hint = None
        if parsed:
            session.commit()

    def _cleanup(self, session: Session, seen: set[str]) -> None:
        """Forget failures of files that are gone (the user fixed them by moving or deleting) and
        keep only the newest `KEEP_SUCCESSES` successful records."""
        # (A fresh query, not `records`: the objects loaded at the start expired when the imports
        # committed, and a Retry from another process may have deleted some meanwhile.)
        gone = [
            row_id
            for row_id, path in session.execute(
                select(WatchedFile.id, WatchedFile.path).where(
                    WatchedFile.outcome.in_(("failed", "ignored"))
                )
            )
            if path not in seen
        ]
        if gone:
            session.execute(delete(WatchedFile).where(WatchedFile.id.in_(gone)))
        session.flush()
        keep = session.scalars(
            select(WatchedFile.id)
            .where(WatchedFile.outcome.in_(SUCCESS_OUTCOMES))
            .order_by(WatchedFile.last_seen.desc(), WatchedFile.id.desc())
            .limit(KEEP_SUCCESSES)
        ).all()
        if len(keep) == KEEP_SUCCESSES:
            session.execute(
                delete(WatchedFile).where(
                    WatchedFile.outcome.in_(SUCCESS_OUTCOMES), WatchedFile.id.not_in(keep)
                )
            )


def _stored_paths(session: Session) -> set[str]:
    """Casefolded relative ``stored_path`` of every imported (``parsed``) statement and tax
    document: the files the app already has (the ones it filed itself and those recorded by
    earlier folder imports). A row that failed is not "known": its file is looked at again once
    its failure record is gone (Retry)."""
    paths: set[str] = set()
    for model in (Statement, TaxDocument):
        query = select(model.stored_path).where(
            model.stored_path.is_not(None), model.status == "parsed"
        )
        for (stored,) in session.execute(query):
            if stored and not Path(stored).is_absolute():
                paths.add(stored.casefold())
    return paths


def _stale(record: WatchedFile, people: list[tuple[int, str]]) -> bool:
    """Whether a record no longer describes the file's situation, although the file is unchanged:
    a path problem that has been solved (a person was created, a folder renamed), or a success
    whose statement or tax document has since been deleted."""
    if record.code in PATH_CODES:
        cls = classify_path(PurePosixPath(record.path).parts, people)
        return cls.code != record.code
    if record.outcome in SUCCESS_OUTCOMES and record.kind is not None:
        return record.statement_id is None and record.tax_document_id is None
    return False


def _resolved(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def retry(session: Session, record_id: int) -> bool:
    """Forget a failed or ignored file's record so the next scan looks at it again."""
    record = session.get(WatchedFile, record_id)
    if record is None or record.outcome not in ("failed", "ignored"):
        return False
    session.delete(record)
    session.commit()
    return True
