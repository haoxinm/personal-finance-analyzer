"""The ingest pipeline (§3.12a, P2-A): two-phase import (A22), reconciliation (A25), drift
detection (A18), dedupe (§3.5), account resolution (D4), and checking->credit transfer pairing
(I11, A26).

```
PHASE 1 (`propose_import`, automatic on upload)
  sha256 -> idempotency check
  -> extract text layer                          [ingest.extract, P1-A]
  -> no text layer?  status='no_text_layer', STOP
  -> locate table bands                          [ingest.layout.locate_table_bands, P0-7]
  -> match issuer against issuers.match_terms    [ingest.issuer_match, §2f.4]
  -> resolve remembered spec + score built-in parsers
  -> status='awaiting_extractor'; return an ImportProposal

PHASE 2 (`confirm_import`, on user confirmation, or automatically when the proposal is confident)
  -> parse -> RawTransaction[]                   [the resolved parser]
  -> normalize + redact                          [ingest.normalize, P1-C]
  -> resolve (or create) the account             [D4]
  -> reconciliation check                        [A25]
  -> drift evaluation against the account's previous statement   [A18]
  -> dedupe against existing rows                [ingest.dedupe, §3.5]
  -> persist statement + transactions in one atomic DB transaction
  -> remember + at least one row: update issuers.default_spec_id / default_parser_id [A23]
  -> pair checking-side card payments with their card-side payment      [I11, A26]
  -> return an ImportResult
```

Kind resolution (the ambiguous `'payment_or_refund'` hint, and the hint-less generic-parser
fallback, including the checking->credit transfer pattern of I11/A26) is `classify.kinds.
resolve_kind` — the single source of truth for those patterns (P2-B). This module calls it once
per row, right before persistence, since `transactions.kind` is `NOT NULL` and every row needs a
concrete `Kind` before the `INSERT`.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from personal_finance_analyzer.classify.kinds import is_spend, resolve_kind
from personal_finance_analyzer.config import Settings, load_settings
from personal_finance_analyzer.core import documents as docs
from personal_finance_analyzer.core.errors import (
    EmptyReparseError,
    ParserError,
    PfaError,
    UnsupportedLayoutError,
)
from personal_finance_analyzer.core.logging import get_logger
from personal_finance_analyzer.core.paths import ensure_home, staging_dir
from personal_finance_analyzer.core.pii import merge_entries
from personal_finance_analyzer.core.types import (
    USER_CLASSIFIED,
    ExtractedDoc,
    ParsedStatement,
    StatementParser,
)
from personal_finance_analyzer.db import documents as doc_files
from personal_finance_analyzer.db.models import Account, Issuer, LayoutSpec, Statement, Transaction
from personal_finance_analyzer.db.pii_terms import user_pii_terms
from personal_finance_analyzer.ingest import issuer_match
from personal_finance_analyzer.ingest.dedupe import (
    DedupeKey,
    compute_dedupe_hash,
    compute_occurrence_indices,
)
from personal_finance_analyzer.ingest.extract import extract, sha256_file
from personal_finance_analyzer.ingest.layout import locate_table_bands
from personal_finance_analyzer.ingest.layout_spec import build_parser_for_row, resolve_spec
from personal_finance_analyzer.ingest.normalize import normalize
from personal_finance_analyzer.ingest.pages import dump_pages, load_pages, validate_pages
from personal_finance_analyzer.ingest.parsers.generic_table import generic_table
from personal_finance_analyzer.ingest.registry import discover_all_parsers
from personal_finance_analyzer.ingest.registry import select as select_parser

logger = get_logger("ingest.pipeline")

#: `statements.status` values this module writes. Not an enforced `Literal` at the ORM layer
#: (the column is plain `TEXT`), but every write goes through one of these constants.
STATUS_AWAITING_EXTRACTOR = "awaiting_extractor"
STATUS_NO_TEXT_LAYER = "no_text_layer"
STATUS_PARSED = "parsed"
STATUS_UNSUPPORTED_LAYOUT = "unsupported_layout"
STATUS_ERROR = "error"

#: A22/D12's fixed copy for a statement with no extractable text layer.
NO_TEXT_LAYER_MESSAGE = (
    "Can't read this PDF — it looks like a scan or an image with no text layer. Personal Finance Analyzer "
    "only supports native digital PDFs. Try downloading the statement directly from your bank's "
    "website rather than a photo or scanned copy."
)

#: The shape warning recorded (and so `layout_drift`) when the extractor returned no rows at all.
NO_TRANSACTIONS_WARNING = "No transactions were found in the selected pages."

#: A parser must score strictly above this for a proposal to be `confident` (§3.12a).
CONFIDENT_SCORE_THRESHOLD = 0.8

#: A drift signal (§2d.1): the winning parser's `detect_score` moved by at least this much from
#: the account's previous statement. Not specified numerically by the plan beyond its own
#: worked example (0.95 -> 0.62); this is a deliberately conservative reading of it.
DETECT_SCORE_DRIFT_THRESHOLD = 0.15

#: ±5 days (I11): the window within which a checking-side transfer is paired with a credit-side
#: payment of equal magnitude.
_TRANSFER_PAIR_WINDOW_DAYS = 5


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


# ------------------------------------------------------------------------------------------------
# §3.12a frozen result types
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportProposal:
    statement_id: int
    status: str  # 'awaiting_extractor' | 'no_text_layer' | 'error' | 'duplicate'
    issuer_id: int | None  # auto-matched issuer, if unambiguous
    parser_id: str | None  # best built-in parser or 'spec_<slug>_v<n>'
    layout_spec_id: int | None
    detect_score: float | None
    confident: bool  # issuer matched AND spec/parser resolved AND score >= 0.8
    #: The extractor is the one the user asked to remember for this issuer.
    remembered: bool = False


@dataclass(frozen=True)
class ImportResult:
    statement_id: int
    inserted: int
    skipped_duplicates: int
    transaction_ids: tuple[int, ...]  # rows inserted; P2-C enqueues classification for them
    reconciliation_delta_minor: int | None  # None when no check was possible
    layout_drift: bool
    warnings: tuple[str, ...]
    #: `reparse` only: hand edits (a category/kind, or a note) put back on the rebuilt rows, and
    #: those that could not be attached to exactly one rebuilt row and were therefore dropped.
    restored_edits: int = 0
    unmatched_edits: int = 0
    #: `reparse(discard_edits=True)` only: hand-edited rows whose edits were deliberately dropped.
    discarded_edits: int = 0
    #: Rows the extractor returned, before dedupe (`inserted + skipped_duplicates`).
    parsed_count: int = 0
    #: `reparse` only: how many transactions the statement had before the rebuild.
    previous_txn_count: int | None = None
    #: `confirm_import(remember=True)` stored this extractor as the issuer's remembered choice.
    remembered: bool = False


# ------------------------------------------------------------------------------------------------
# Durable staging: every uploaded PDF is written under the data dir at propose time, keyed by its
# content hash (`incoming/<sha256>.pdf`), so `confirm_import`/`reparse` can re-read it from *any*
# process — the CLI's A33 "leave it `awaiting_extractor` and print the command that confirms it" is
# a different process from the one that ran `propose_import`, and the server can restart in
# between either way.
#
# `[privacy] store_pdf_copies` controls whether the PDF is *kept*: when it is, Phase 2 reaching a
# terminal outcome files the staged copy in the documents directory under a readable name (the
# person, institution, account and statement month are only known by then) and records that path
# in `statements.stored_path`; when it is `False` the staged file is deleted instead and
# `stored_path` stays NULL. Until then `stored_path` is NULL and the staged copy is the PDF.
# ------------------------------------------------------------------------------------------------


def _staged_pdf_path(file_sha256: str) -> Path:
    return staging_dir() / f"{file_sha256}.pdf"


def _stage_pdf(pdf_path: Path, file_sha256: str) -> Path:
    """Ensure a durable copy of ``pdf_path`` exists at its content-hash path, and return it.
    Idempotent: a same-content file re-staged is a no-op write."""
    ensure_home()
    dest = _staged_pdf_path(file_sha256)
    if not dest.exists():
        dest.write_bytes(pdf_path.read_bytes())
    return dest


def _adopt_in_place(statement: Statement, rel: str) -> None:
    """Record ``rel`` (a file the user put in the documents folder) as the statement's PDF unless
    the row already keeps a copy that is still on disk."""
    current = docs.resolve(statement.stored_path) if statement.stored_path else None
    if current is None or not current.is_file():
        statement.stored_path = rel
        statement.pdf_in_place = True


def _finish_pdf(
    session: Session,
    statement: Statement,
    source: Path,
    settings: Settings,
    *,
    in_place: bool = False,
) -> None:
    """Phase 2 reached a terminal outcome: file the PDF under its readable name when
    `[privacy] store_pdf_copies` asks to keep it, and remove the staged upload either way (unless
    filing failed, in which case the staged copy stays and keeps the statement usable).

    ``in_place``: the PDF is a file the user put in the documents folder themselves (folder
    imports, `watch/`) and ``statement.stored_path`` already names it: it is neither copied nor
    renamed, whatever `store_pdf_copies` says."""
    if in_place:
        _staged_pdf_path(statement.file_sha256).unlink(missing_ok=True)
        return
    if (
        settings.privacy.store_pdf_copies
        and doc_files.file_statement(session, statement, source) is None
    ):
        return
    _staged_pdf_path(statement.file_sha256).unlink(missing_ok=True)


# ------------------------------------------------------------------------------------------------
# Phase 1 — propose_import
# ------------------------------------------------------------------------------------------------


def propose_import(
    session: Session,
    pdf_path: Path,
    *,
    user_id: int,
    original_name: str,
    in_place: str | None = None,
) -> ImportProposal:
    """Phase 1 of A22: extract text, locate the table, match the issuer, score parsers, and
    commit a `statements` row at `status='awaiting_extractor'`. Never parses transactions.

    Idempotency (I9): if a statement with this file's SHA-256 already exists, nothing is
    re-extracted or re-scored. A `status='parsed'` row is reported back as `status='duplicate'`
    (this upload is a no-op); the upload's staged PDF, if any, is deleted (the kept copy, if any,
    is already filed). Any
    other existing status (including `'awaiting_extractor'` itself) is reported as-is (a failed
    `'error'`/`'unsupported_layout'` row is reopened as `'awaiting_extractor'`, keeping its
    `error_detail`, and is never `confident`), and the
    file is (re-)staged and `user_id` refreshed on the existing row — the recovery path for a
    statement whose Phase 2 previously failed, or whose staged copy was deleted because
    `[privacy] store_pdf_copies` is off: re-proposing the same bytes makes `confirm_import`/
    `reparse` runnable again from any process, since both derive the user and the file from this
    row rather than from anything kept only in memory.

    Args:
        session: an open session. This function commits the `statements` row it writes.
        pdf_path: path to the uploaded PDF, already saved to local disk by the caller (upload
            validation — size cap, magic-byte check, A12 — is the API layer's job, not this
            module's: `ingest/` takes a trusted local path).
        user_id: the user this import is for (D4). Persisted on `statements.user_id` so Phase 2 —
            a separate call, possibly in a different process or after a restart — can resolve or
            create the account without any process-local state.
        original_name: the user-supplied filename, stored verbatim for display. The *stored*
            copy's filename is always the content hash, never this value (A12).
        in_place: folder imports (`watch/`) only: the path, relative to the documents directory,
            of a file the user put there themselves. It is recorded as ``stored_path`` at once and
            nothing is staged or copied; pass ``keep_in_place=True`` to `confirm_import` so it is
            not renamed either.

    Returns:
        An `ImportProposal` describing what Phase 2 would do if confirmed now.
    """
    file_sha256 = sha256_file(pdf_path)
    existing = (
        session.execute(select(Statement).where(Statement.file_sha256 == file_sha256))
        .scalars()
        .first()
    )
    if existing is not None:
        retry_after_failure = existing.status in (STATUS_UNSUPPORTED_LAYOUT, STATUS_ERROR)
        rescored: _Proposed | None = None
        if existing.status != STATUS_PARSED:
            if in_place is not None:
                _adopt_in_place(existing, in_place)
            else:
                _stage_pdf(pdf_path, file_sha256)
            existing.user_id = user_id
            failed_parser = existing.parser_id if retry_after_failure else None
            if retry_after_failure:
                # A failed built-in parse is retryable: re-proposing reopens the statement
                # (the failure message stays in `error_detail` as a warning) and it is never
                # reported as confident, so nothing auto-confirms the parser that just failed.
                existing.status = STATUS_AWAITING_EXTRACTOR
            if existing.status == STATUS_AWAITING_EXTRACTOR:
                # Re-score: the issuers, specs and remembered choices may have changed since the
                # proposal was recorded.
                rescored = _propose_extractor(
                    session,
                    extract(pdf_path),
                    load_settings(),
                    # The extractor that just failed is not proposed again.
                    exclude=frozenset({failed_parser}) if failed_parser else frozenset(),
                )
                _apply_proposal(existing, rescored)
            session.commit()
        if existing.status == STATUS_PARSED:
            # Nothing to confirm and the kept copy (if any) is already filed under its readable
            # name: the upload that arrived with this duplicate is just a stray hash-named copy.
            _staged_pdf_path(file_sha256).unlink(missing_ok=True)
        status = "duplicate" if existing.status == STATUS_PARSED else existing.status
        return ImportProposal(
            statement_id=existing.id,
            status=status,
            issuer_id=existing.issuer_id,
            parser_id=existing.parser_id,
            layout_spec_id=existing.layout_spec_id,
            detect_score=existing.detect_score,
            confident=not retry_after_failure
            and _is_confident(existing.issuer_id, existing.detect_score, existing.status)
            and (rescored is None or rescored.confident),
            remembered=rescored is not None and rescored.remembered,
        )

    doc = extract(pdf_path)
    settings = load_settings()
    if in_place is None:
        _stage_pdf(pdf_path, file_sha256)

    if not doc.has_text_layer:
        statement = Statement(
            user_id=user_id,
            file_sha256=file_sha256,
            original_name=original_name,
            stored_path=in_place,
            pdf_in_place=in_place is not None,
            page_count=doc.page_count,
            status=STATUS_NO_TEXT_LAYER,
            error_detail=NO_TEXT_LAYER_MESSAGE,
            ingested_at=_utcnow_iso(),
        )
        session.add(statement)
        session.commit()
        _finish_pdf(
            session,
            statement,
            pdf_path if in_place is not None else _staged_pdf_path(file_sha256),
            settings,
            in_place=in_place is not None,
        )
        return ImportProposal(
            statement_id=statement.id,
            status=STATUS_NO_TEXT_LAYER,
            issuer_id=None,
            parser_id=None,
            layout_spec_id=None,
            detect_score=None,
            confident=False,
        )

    proposed = _propose_extractor(session, doc, settings)

    statement = Statement(
        user_id=user_id,
        file_sha256=file_sha256,
        original_name=original_name,
        stored_path=in_place,
        pdf_in_place=in_place is not None,
        page_count=doc.page_count,
        status=STATUS_AWAITING_EXTRACTOR,
        ingested_at=_utcnow_iso(),
    )
    _apply_proposal(statement, proposed)
    session.add(statement)
    session.commit()

    return ImportProposal(
        statement_id=statement.id,
        status=STATUS_AWAITING_EXTRACTOR,
        issuer_id=proposed.issuer_id,
        parser_id=proposed.parser_id,
        layout_spec_id=proposed.layout_spec_id,
        detect_score=proposed.detect_score,
        confident=proposed.confident,
        remembered=proposed.remembered,
    )


@dataclass(frozen=True)
class _Proposed:
    """What `_propose_extractor` decided for a document."""

    issuer_id: int | None
    parser_id: str
    parser_version: str
    detect_score: float | None
    layout_spec_id: int | None
    confident: bool
    #: The extractor is the issuer's remembered choice (`issuers.default_spec_id` /
    #: `default_parser_id`), not just the best-scoring one.
    remembered: bool


def _propose_extractor(
    session: Session,
    doc: ExtractedDoc,
    settings: Settings,
    *,
    exclude: frozenset[str] = frozenset(),
) -> _Proposed:
    """Match the issuer and choose the extractor to propose for ``doc``.

    Order: the issuer's remembered choice when it is *confident* for this document (its
    `detect()` score reaches `CONFIDENT_SCORE_THRESHOLD`), else the configured LLM default, else
    the best-scoring built-in/spec. A remembered LLM has no score, so it never beats a confident
    local extractor: it is proposed only when normal scoring is not confident. ``exclude`` holds
    parser ids that must not be proposed (the extractor that just failed on this statement).
    Pure: it reads the database but writes nothing (the LLM is never called)."""
    issuer_id = issuer_match.match_issuer(session, doc, locate_table_bands(doc))
    selection = _remembered_selection(session, doc, issuer_id, settings, exclude=exclude)
    if selection is not None and selection[0] != "llm":
        parser_id, version, score, spec_id = selection
        return _Proposed(
            issuer_id=issuer_id,
            parser_id=parser_id,
            parser_version=version,
            detect_score=score,
            layout_spec_id=spec_id,
            confident=_is_confident(issuer_id, score, STATUS_AWAITING_EXTRACTOR),
            remembered=True,
        )
    scored = _scored_proposal(session, doc, issuer_id, exclude)
    if selection is not None:
        if scored.confident:
            return scored  # a confident local extractor beats the remembered LLM
        parser_id, version, score, spec_id = selection
        return _Proposed(
            issuer_id=issuer_id,
            parser_id=parser_id,
            parser_version=version,
            detect_score=score,
            layout_spec_id=spec_id,
            confident=False,
            remembered=True,
        )
    if settings.ingest.default_extractor == "llm" and settings.llm.is_configured:
        # LLM imports are never auto-confirmed: the user picks the pages to send first.
        from personal_finance_analyzer.llm.statement_parser import LLM_PARSER_ID, LLMStatementParser

        return _Proposed(
            issuer_id=issuer_id,
            parser_id=LLM_PARSER_ID,
            parser_version=LLMStatementParser.version,
            detect_score=None,
            layout_spec_id=None,
            confident=False,
            remembered=False,
        )
    return scored


def _scored_proposal(
    session: Session, doc: ExtractedDoc, issuer_id: int | None, exclude: frozenset[str]
) -> _Proposed:
    selected, score, layout_spec_id = _select_parser(session, doc, issuer_id, exclude=exclude)
    return _Proposed(
        issuer_id=issuer_id,
        parser_id=selected.id,
        parser_version=selected.version,
        detect_score=score,
        layout_spec_id=layout_spec_id,
        confident=_is_confident(issuer_id, score, STATUS_AWAITING_EXTRACTOR),
        remembered=False,
    )


def _apply_proposal(statement: Statement, proposed: _Proposed) -> None:
    """Record a proposal on the statement row (never touches ``selected_pages``)."""
    statement.issuer_id = proposed.issuer_id
    statement.layout_spec_id = proposed.layout_spec_id
    statement.parser_id = proposed.parser_id
    statement.parser_version = proposed.parser_version
    statement.detect_score = proposed.detect_score


def _newest_approved_version(session: Session, row: LayoutSpec) -> LayoutSpec | None:
    """The highest approved version of the spec ``row`` belongs to (same name and issuer): a
    revision approved after the user remembered ``row`` supersedes it."""
    stmt = (
        select(LayoutSpec)
        .where(
            LayoutSpec.name == row.name,
            LayoutSpec.approved.is_(True),
            LayoutSpec.issuer_id.is_(None)
            if row.issuer_id is None
            else LayoutSpec.issuer_id == row.issuer_id,
        )
        .order_by(LayoutSpec.version.desc())
    )
    return session.execute(stmt).scalars().first()


def _remembered_selection(
    session: Session,
    doc: ExtractedDoc,
    issuer_id: int | None,
    settings: Settings,
    *,
    exclude: frozenset[str] = frozenset(),
) -> tuple[str, str, float | None, int | None] | None:
    """The issuer's remembered extractor as ``(parser_id, version, detect_score, spec_id)``, or
    ``None`` when there is none or it cannot be used: a spec with no approved version left, a
    parser that is no longer registered, the LLM when it is not configured, an extractor in
    ``exclude``, or a spec/parser that is not *confident* for this document (score below
    `CONFIDENT_SCORE_THRESHOLD`). Selection then falls back to normal scoring. A remembered spec
    resolves to the newest approved version of the same spec. The LLM has no score and is
    returned as ``("llm", ..., None, None)``; the caller decides whether it may win."""
    issuer = session.get(Issuer, issuer_id) if issuer_id is not None else None
    if issuer is None:
        return None
    if issuer.default_spec_id is not None:
        remembered_row = session.get(LayoutSpec, issuer.default_spec_id)
        row = _newest_approved_version(session, remembered_row) if remembered_row else None
        if row is None or row.parser_id in exclude:
            return None
        spec_parser = cast(StatementParser, build_parser_for_row(row))
        score = _detect_score(spec_parser, doc)
        if score < CONFIDENT_SCORE_THRESHOLD:
            return None
        return spec_parser.id, spec_parser.version, score, row.id
    remembered = issuer.default_parser_id
    if remembered is None or remembered in exclude:
        return None
    if remembered == "llm":
        if not settings.llm.is_configured:
            return None
        from personal_finance_analyzer.llm.statement_parser import LLM_PARSER_ID, LLMStatementParser

        return LLM_PARSER_ID, LLMStatementParser.version, None, None
    for parser in (*discover_all_parsers(), generic_table):
        if parser.id == remembered:
            score = _detect_score(parser, doc)
            if score < CONFIDENT_SCORE_THRESHOLD:
                return None
            return parser.id, parser.version, score, None
    return None


def _is_confident(issuer_id: int | None, score: float | None, status: str) -> bool:
    """§3.12a: "issuer matched AND spec/parser resolved AND score >= 0.8"."""
    return (
        status == STATUS_AWAITING_EXTRACTOR
        and issuer_id is not None
        and score is not None
        and score >= CONFIDENT_SCORE_THRESHOLD
    )


def _detect_score(parser: StatementParser, doc: ExtractedDoc) -> float:
    """Mirrors `registry._safe_detect`: a raising `detect()` scores `0.0` rather than crashing
    selection (§3.1 requires `detect()` to never raise, but this module does not trust that)."""
    try:
        return parser.detect(doc)
    except Exception:  # detect() must never raise; treat a violation as no confidence
        logger.warning("parser %r raised in detect(); scoring 0.0", parser.id, exc_info=True)
        return 0.0


def _select_parser(
    session: Session,
    doc: ExtractedDoc,
    issuer_id: int | None,
    *,
    exclude: frozenset[str] = frozenset(),
) -> tuple[StatementParser, float, int | None]:
    """Score every built-in/entry-point parser plus, when an issuer matched, every approved
    layout spec remembered for that issuer (§2f.1, §2f.2) — for every account type, since the
    account type is not known until parsing runs and a spec's own `account_type` is usually a
    stronger signal than guessing one first. Returns the winning parser, its `detect()` score,
    and the `layout_specs.id` it came from (`None` for a built-in parser)."""
    candidates: list[StatementParser] = [p for p in discover_all_parsers() if p.id not in exclude]
    spec_by_parser_id: dict[str, LayoutSpec] = {}
    if issuer_id is not None:
        for account_type in ("credit", "checking", "savings"):
            spec_row = resolve_spec(session, issuer_id=issuer_id, account_type=account_type)
            if (
                spec_row is not None
                and spec_row.parser_id not in spec_by_parser_id
                and spec_row.parser_id not in exclude
            ):
                spec_by_parser_id[spec_row.parser_id] = spec_row
                candidates.append(cast(StatementParser, build_parser_for_row(spec_row)))

    selected = select_parser(doc, candidates)
    score = _detect_score(selected, doc)
    layout_spec_id = spec_by_parser_id[selected.id].id if selected.id in spec_by_parser_id else None
    return selected, score, layout_spec_id


# ------------------------------------------------------------------------------------------------
# Phase 2 — confirm_import
# ------------------------------------------------------------------------------------------------


def confirm_import(
    session: Session,
    statement_id: int,
    *,
    issuer_id: int,
    parser_id: str | None,
    layout_spec_id: int | None,
    remember: bool,
    pages: Sequence[int] | None = None,
    keep_in_place: bool = False,
) -> ImportResult:
    """Phase 2 of A22: parse, normalize, reconcile, dedupe, and persist a statement's
    transactions in one atomic transaction.

    Args:
        session: an open session. Commits the statement + transaction rows atomically on success,
            and commits an `error`/`unsupported_layout` status update on failure before
            re-raising, so a re-import of the same bad file returns the same clear message
            instead of reprocessing (the WP's "failure handling" requirement).
        statement_id: a statement previously returned by `propose_import`.
        issuer_id: the issuer to pin to this statement (auto-matched or user-picked; never
            ambiguous by the time this is called).
        parser_id: a built-in parser id to use, or `None` when `layout_spec_id` selects one.
        layout_spec_id: an approved `layout_specs.id` to use, or `None` when `parser_id` selects
            one. Exactly one of `parser_id`/`layout_spec_id` must be given.
        remember: when `True` and the extractor returned at least one row, stores it as the
            issuer's remembered choice (`issuers.default_spec_id` for a layout spec, else
            `issuers.default_parser_id`; setting one clears the other) and updates
            `issuers.last_used_at` (A23). `ImportResult.remembered` reports whether it was stored.
        pages: 1-based pages to parse (deduped, sorted, validated against the PDF's page count)
            and saved to `statements.selected_pages`; `None` keeps the saved selection (default:
            all pages). Every parser, template or LLM, sees only the selected pages.
        keep_in_place: the statement's PDF is a file the user put in the documents folder
            (`propose_import(in_place=...)`): leave it where it is, under its own name.

    Returns:
        An `ImportResult`. `inserted`/`skipped_duplicates`/`transaction_ids` all refer to *this*
        call — reconfirming an already-`parsed` statement is a no-op that reports zero of each.

    Raises:
        PfaError: the statement was not found, has no `user_id` (every statement
            `propose_import` writes has one; only possible for a row constructed some other way),
            or its staged PDF is no longer on disk (re-propose the same file to restage it).
        ValueError: neither or both of `parser_id`/`layout_spec_id` were given.
        UnsupportedLayoutError: the generic parser found too few transactions to be usable.
            `statements.status` is set to `'unsupported_layout'` (message in `error_detail`)
            before this is re-raised. The staged PDF is kept and the statement stays
            confirmable: calling this again with another `parser_id` (e.g. ``"llm"``) works.
        ParserError: the parser could not otherwise parse the statement (e.g. an unresolvable
            year-less date). `statements.status` is set to `'error'` before this is re-raised;
            the staged PDF is kept and the statement can be confirmed again.
    """
    statement = session.get(Statement, statement_id)
    if statement is None:
        raise PfaError(f"no statement with id={statement_id}")

    if statement.status == STATUS_PARSED:
        return ImportResult(
            statement_id=statement.id,
            inserted=0,
            skipped_duplicates=statement.txn_count or 0,
            transaction_ids=(),
            reconciliation_delta_minor=None,
            layout_drift=False,
            warnings=(),
        )

    if (parser_id is None) == (layout_spec_id is None):
        raise ValueError("exactly one of parser_id or layout_spec_id must be given")

    user_id, path = _resolve_source(statement)
    settings = load_settings()
    full_doc = extract(path)
    doc = _apply_page_selection(statement, full_doc, pages)
    parser = _resolve_parser(
        session, parser_id=parser_id, layout_spec_id=layout_spec_id, user_id=user_id
    )

    try:
        parsed = parser.parse(doc)
    except UnsupportedLayoutError as exc:
        # A failing parser is a warning, not a dead end: the staged PDF is kept so the user can
        # confirm again with another extractor (e.g. the LLM). A statement never retried keeps its
        # staged copy (small, local; deleted once a retry reaches a terminal outcome).
        _mark_failed(session, statement, STATUS_UNSUPPORTED_LAYOUT, str(exc))
        raise
    except ParserError as exc:
        _mark_failed(session, statement, STATUS_ERROR, str(exc))
        raise

    result = _persist_parsed_statement(
        session,
        statement=statement,
        parsed=parsed,
        user_id=user_id,
        issuer_id=issuer_id,
        parser=parser,
        layout_spec_id=layout_spec_id,
        remember=remember,
    )
    _finish_pdf(session, statement, path, settings, in_place=keep_in_place)
    return result


def _apply_page_selection(
    statement: Statement, doc: ExtractedDoc, pages: Sequence[int] | None
) -> ExtractedDoc:
    """Validate and save an explicit ``pages`` selection on ``statement`` (if given), then return
    ``doc`` restricted to the statement's saved selection (all pages when none is saved)."""
    if pages is not None:
        statement.selected_pages = dump_pages(validate_pages(pages, doc.page_count))
    return doc.select_pages(load_pages(statement.selected_pages))


def _resolve_source(statement: Statement) -> tuple[int, Path]:
    """Recover `(user_id, pdf_path)` for Phase 2 from the `statements` row and the data dir
    alone — never from anything only a Python process could remember (see the module docstring
    on durable staging)."""
    if statement.user_id is None:  # pragma: no cover - defensive; propose_import always sets it
        raise PfaError(
            f"statement {statement.id} has no user_id; it was not created by propose_import"
        )
    path = docs.kept_pdf(statement.stored_path, statement.file_sha256) or docs.staged_pdf(
        statement.file_sha256
    )
    if path is None:
        raise PfaError(
            f"statement {statement.id} has no staged PDF on disk; re-propose the same file to "
            "restage it before confirming"
        )
    return statement.user_id, path


def _mark_failed(session: Session, statement: Statement, status: str, error_detail: str) -> None:
    statement.status = status
    statement.error_detail = error_detail
    session.commit()


def _resolve_parser(
    session: Session,
    *,
    parser_id: str | None,
    layout_spec_id: int | None,
    user_id: int | None = None,
) -> StatementParser:
    if layout_spec_id is not None:
        row = session.get(LayoutSpec, layout_spec_id)
        if row is None:
            raise PfaError(f"no layout_specs row with id={layout_spec_id}")
        return cast(StatementParser, build_parser_for_row(row))

    if parser_id == "llm":
        # Lazy: the LLM statement parser is one of only two modules allowed to reach the HTTP
        # client; `ingest/` itself never imports it at module scope.
        from personal_finance_analyzer.llm.statement_parser import LLMStatementParser

        settings = load_settings()
        return LLMStatementParser(settings.llm, user_pii_terms(session, settings, user_id))
    if parser_id == generic_table.id:
        return generic_table
    for candidate in discover_all_parsers():
        if candidate.id == parser_id:
            return candidate
    raise UnsupportedLayoutError(f"no parser registered with id={parser_id!r}")


def extractor_exists(
    session: Session, *, parser_id: str | None, layout_spec_id: int | None
) -> bool:
    """Whether `_resolve_parser` could resolve this extractor (cheap; builds no parser, makes no
    LLM call). Lets the API reject an unusable recorded extractor up front."""
    if layout_spec_id is not None:
        return session.get(LayoutSpec, layout_spec_id) is not None
    if parser_id in ("llm", generic_table.id):
        return True
    return any(candidate.id == parser_id for candidate in discover_all_parsers())


# ------------------------------------------------------------------------------------------------
# Persistence: normalize, resolve the account, reconcile, evaluate drift, dedupe-insert
# ------------------------------------------------------------------------------------------------


def _persist_parsed_statement(
    session: Session,
    *,
    statement: Statement,
    parsed: ParsedStatement,
    user_id: int,
    issuer_id: int,
    parser: StatementParser,
    layout_spec_id: int | None,
    remember: bool,
    commit: bool = True,
) -> ImportResult:
    """Insert `parsed`'s rows and mark `statement` parsed. Commits (twice: the rows, then the
    transfer pairing) unless `commit=False`, which `reparse` uses so the rebuild and the restore of
    hand edits are one transaction the caller commits or rolls back."""
    settings = load_settings()
    account_type = parsed.account_hint.account_type or parser.account_type
    mask = parsed.account_hint.mask or ""
    currency = parsed.account_hint.currency or settings.ingest.default_currency

    issuer = session.get(Issuer, issuer_id)
    if issuer is None:
        raise PfaError(f"no issuers row with id={issuer_id}")
    account = _resolve_account(
        session,
        user_id=user_id,
        issuer_id=issuer_id,
        account_type=account_type,
        mask=mask,
        currency=currency,
        issuer_name=issuer.name,
    )

    pii_terms = _gather_pii_terms(session, settings, user_id)
    issuer_match_terms = _gather_issuer_match_terms(session)

    rows: list[dict[str, object]] = []
    dedupe_keys: list[DedupeKey] = []
    for raw in parsed.transactions:
        normalized = normalize(raw.description, pii_terms)
        kind = resolve_kind(
            kind_hint=raw.kind_hint,
            description_clean=normalized.description_clean,
            amount_minor=raw.amount_minor,
            account_type=account_type,
            issuer_match_terms=issuer_match_terms,
        )
        posted_date_iso = raw.posted_date.isoformat()
        dedupe_keys.append(
            DedupeKey(
                account_id=account.id,
                posted_date=posted_date_iso,
                amount_minor=raw.amount_minor,
                description_raw=raw.description,
            )
        )
        rows.append(
            {
                "statement_id": statement.id,
                "account_id": account.id,
                "user_id": user_id,
                "posted_date": posted_date_iso,
                "transaction_date": raw.transaction_date.isoformat()
                if raw.transaction_date
                else None,
                "description_raw": raw.description,
                "description_clean": normalized.description_clean,
                "merchant_key": normalized.merchant_key,
                "issuer_category": raw.issuer_category,
                "amount_minor": raw.amount_minor,
                "currency": raw.currency,
                "fx_amount_minor": raw.fx_amount_minor,
                "fx_currency": raw.fx_currency,
                "fx_rate": raw.fx_rate,
                "kind": kind,
                "is_spend": is_spend(kind),
            }
        )

    occurrence_indices = compute_occurrence_indices(dedupe_keys)
    for row, key, occurrence_index in zip(rows, dedupe_keys, occurrence_indices, strict=True):
        row["dedupe_hash"] = compute_dedupe_hash(key, occurrence_index)

    reconciliation_delta, reconciliation_mismatch, section_warnings = _reconcile(
        parsed, account_type
    )

    previous = _previous_statement(session, account.id, exclude_statement_id=statement.id)
    empty_warning = (NO_TRANSACTIONS_WARNING,) if not parsed.transactions else ()
    layout_drift, shape_warnings = _evaluate_drift(
        parsed_warnings=parsed.warnings + section_warnings + empty_warning,
        reconciliation_mismatch=reconciliation_mismatch,
        detect_score=statement.detect_score,
        previous_detect_score=previous.detect_score if previous is not None else None,
    )

    inserted_ids = _insert_transactions(session, rows)

    statement.status = STATUS_PARSED
    statement.error_detail = None
    statement.parser_id = parser.id
    statement.parser_version = parser.version
    statement.account_id = account.id
    statement.issuer_id = issuer_id
    statement.layout_spec_id = layout_spec_id
    statement.period_start = parsed.period_start.isoformat() if parsed.period_start else None
    statement.period_end = parsed.period_end.isoformat() if parsed.period_end else None
    statement.txn_count = len(inserted_ids)
    statement.parsed_row_count = len(parsed.transactions)
    statement.parsed_total_minor = sum(t.amount_minor for t in parsed.transactions)
    statement.stated_total_minor = parsed.stated_total_minor
    statement.opening_balance_minor = parsed.opening_balance_minor
    statement.closing_balance_minor = parsed.closing_balance_minor
    statement.shape_warnings = json.dumps(list(shape_warnings)) if shape_warnings else None

    # "Remember" is an explicit decision of the user, so a reconciliation mismatch or drift does
    # not block it; an extractor that found nothing is not worth remembering.
    remembered = remember and bool(parsed.transactions)
    if remembered:
        if layout_spec_id is not None:
            issuer.default_spec_id = layout_spec_id
            issuer.default_parser_id = None
        else:
            issuer.default_parser_id = parser.id
            issuer.default_spec_id = None
        issuer.last_used_at = _utcnow_iso()

    if commit:
        session.commit()
    else:
        session.flush()

    if account_type in ("checking", "savings"):
        _pair_transfers(session, account=account, user_id=user_id, inserted_ids=inserted_ids)
        if commit:
            session.commit()
        else:
            session.flush()

    return ImportResult(
        statement_id=statement.id,
        inserted=len(inserted_ids),
        skipped_duplicates=len(rows) - len(inserted_ids),
        transaction_ids=tuple(inserted_ids),
        reconciliation_delta_minor=reconciliation_delta,
        layout_drift=layout_drift,
        warnings=shape_warnings,
        parsed_count=len(rows),
        remembered=remembered,
    )


def _resolve_account(
    session: Session,
    *,
    user_id: int,
    issuer_id: int,
    account_type: str,
    mask: str,
    currency: str,
    issuer_name: str,
) -> Account:
    """Resolve `(user_id, issuer_id, mask, account_type)` to an `Account`, creating it if absent
    (D4)."""
    existing = (
        session.execute(
            select(Account).where(
                Account.user_id == user_id,
                Account.issuer_id == issuer_id,
                Account.mask == mask,
                Account.account_type == account_type,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing

    display_name = f"{issuer_name} {account_type}" + (f" ...{mask}" if mask else "")
    account = Account(
        user_id=user_id,
        issuer_id=issuer_id,
        account_type=account_type,
        display_name=display_name,
        mask=mask,
        currency=currency,
    )
    session.add(account)
    session.flush()
    return account


def _gather_pii_terms(session: Session, settings: Settings, user_id: int) -> tuple[str, ...]:
    """A17: issuer names and `match_terms` are added to the redaction term list, alongside the
    configured `[privacy] pii_terms` and this user's `pii_aliases` (D10/A11)."""
    extra: list[str] = []
    for issuer in session.execute(select(Issuer)).scalars():
        extra.append(issuer.name)
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            extra.extend(json.loads(issuer.match_terms))
    return merge_entries(user_pii_terms(session, settings, user_id), extra)


def _gather_issuer_match_terms(session: Session) -> tuple[str, ...]:
    """Every configured issuer's `match_terms` (I1b, local-only), flattened for
    `classify.kinds.resolve_kind`'s issuer-proximity check (A26). Never egressed."""
    terms: set[str] = set()
    for issuer in session.execute(select(Issuer)).scalars():
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            terms.update(json.loads(issuer.match_terms))
    return tuple(sorted(terms))


def _insert_transactions(session: Session, rows: list[dict[str, object]]) -> list[int]:
    """Bulk-insert ``rows``, skipping any that collide with an existing
    `(account_id, dedupe_hash)` (§3.5) via `INSERT ... ON CONFLICT DO NOTHING`, and return the ids
    that were actually inserted (via `RETURNING`, which reports only the rows the conflict clause
    did not drop)."""
    if not rows:
        return []
    stmt = (
        sqlite_insert(Transaction)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["account_id", "dedupe_hash"])
        .returning(Transaction.id)
    )
    result = session.execute(stmt)
    return [row[0] for row in result]


# ------------------------------------------------------------------------------------------------
# Reconciliation (A25) and drift evaluation (A18)
# ------------------------------------------------------------------------------------------------


def _reconcile(
    parsed: ParsedStatement, account_type: str
) -> tuple[int | None, bool, tuple[str, ...]]:
    """A25: the balance equation is the primary check; `stated_total_minor` is a fallback only
    when no balances were read. Section totals (A15) additionally localize a mismatch to one
    section, recorded as extra warnings, but never override the primary check's verdict.

    Returns:
        ``(delta_minor, mismatch, warnings)``. ``delta_minor`` is `None` when neither check was
        possible.
    """
    total = sum(t.amount_minor for t in parsed.transactions)
    delta: int | None
    if parsed.opening_balance_minor is not None and parsed.closing_balance_minor is not None:
        if account_type == "credit":
            expected = parsed.closing_balance_minor - parsed.opening_balance_minor
        else:
            expected = parsed.opening_balance_minor - parsed.closing_balance_minor
        delta = total - expected
    elif parsed.stated_total_minor is not None:
        delta = total - parsed.stated_total_minor
    else:
        delta = None

    mismatch = delta is not None and delta != 0
    warnings: list[str] = []
    if mismatch:
        warnings.append(f"reconciliation delta is {delta} minor units (non-zero)")

    if parsed.section_totals:
        actual_by_section: dict[str, int] = {}
        for txn in parsed.transactions:
            label = txn.section or ""
            actual_by_section[label] = actual_by_section.get(label, 0) + txn.amount_minor
        for label, stated in parsed.section_totals:
            actual = actual_by_section.get(label, 0)
            if actual != stated:
                warnings.append(
                    f"section {label!r} totals {actual} but the statement states {stated}"
                )

    return delta, mismatch, tuple(warnings)


def _previous_statement(
    session: Session, account_id: int, *, exclude_statement_id: int
) -> Statement | None:
    """The account's most recently ingested *other* parsed statement, for drift comparison."""
    return (
        session.execute(
            select(Statement)
            .where(
                Statement.account_id == account_id,
                Statement.status == STATUS_PARSED,
                Statement.id != exclude_statement_id,
            )
            .order_by(Statement.ingested_at.desc())
        )
        .scalars()
        .first()
    )


def _evaluate_drift(
    *,
    parsed_warnings: tuple[str, ...],
    reconciliation_mismatch: bool,
    detect_score: float | None,
    previous_detect_score: float | None,
) -> tuple[bool, tuple[str, ...]]:
    """§2d.1: any of a reconciliation delta, a parser shape-assertion failure, or a detect-score
    decay from the account's previous statement trips `layout_drift`."""
    warnings = list(parsed_warnings)
    drift = bool(parsed_warnings) or reconciliation_mismatch

    if (
        detect_score is not None
        and previous_detect_score is not None
        and abs(detect_score - previous_detect_score) >= DETECT_SCORE_DRIFT_THRESHOLD
    ):
        drift = True
        warnings.append(
            f"detect_score changed from {previous_detect_score:.2f} to {detect_score:.2f} "
            "since this account's previous statement"
        )

    return drift, tuple(warnings)


# ------------------------------------------------------------------------------------------------
# Transfer pairing (I11, A26)
# ------------------------------------------------------------------------------------------------


def _pair_transfers(
    session: Session, *, account: Account, user_id: int, inserted_ids: list[int]
) -> None:
    """After persisting a checking/savings statement, link each newly inserted `transfer`
    transaction to a same-user credit-account `payment` of equal magnitude within
    ±`_TRANSFER_PAIR_WINDOW_DAYS` days (I11, A26). Both sides are already excluded from spend by
    `kind`; the link (stored in `notes`, no schema change per the plan) lets a *missed* pattern
    surface later as an unmatched payment rather than silently double-counting."""
    if not inserted_ids:
        return
    transfers = (
        session.execute(
            select(Transaction).where(
                Transaction.id.in_(inserted_ids), Transaction.kind == "transfer"
            )
        )
        .scalars()
        .all()
    )
    if not transfers:
        return

    credit_account_ids = (
        session.execute(
            select(Account.id).where(Account.user_id == user_id, Account.account_type == "credit")
        )
        .scalars()
        .all()
    )
    if not credit_account_ids:
        return

    for transfer in transfers:
        if transfer.notes is not None:
            continue
        window_start = _shift_iso_date(transfer.posted_date, -_TRANSFER_PAIR_WINDOW_DAYS)
        window_end = _shift_iso_date(transfer.posted_date, _TRANSFER_PAIR_WINDOW_DAYS)
        candidate = (
            session.execute(
                select(Transaction).where(
                    Transaction.account_id.in_(credit_account_ids),
                    Transaction.kind == "payment",
                    Transaction.amount_minor == -transfer.amount_minor,
                    Transaction.posted_date >= window_start,
                    Transaction.posted_date <= window_end,
                    Transaction.notes.is_(None),
                )
            )
            .scalars()
            .first()
        )
        if candidate is not None:
            transfer.notes = json.dumps({"transfer_pair_id": candidate.id})
            candidate.notes = json.dumps({"transfer_pair_id": transfer.id})


def _shift_iso_date(iso_date: str, days: int) -> str:
    return (date.fromisoformat(iso_date) + timedelta(days=days)).isoformat()


# ------------------------------------------------------------------------------------------------
# reparse and reassign
# ------------------------------------------------------------------------------------------------


def reparse(
    session: Session,
    statement_id: int,
    *,
    parser_id: str | None,
    layout_spec_id: int | None,
    pages: Sequence[int] | None = None,
    discard_edits: bool = False,
    allow_empty: bool = False,
) -> ImportResult:
    """Re-run a *previously confirmed* statement with a different extractor, rebuilding its rows
    (the API surface's ``POST /api/statements/{id}/reparse``).

    Requires a durable PDF copy (``statements.stored_path``, `[privacy] store_pdf_copies`) since,
    unlike `confirm_import`, there is no fresh upload to fall back to for an older statement.

    Does not commit (per §3.12a: this function is not documented as committing); the caller
    controls the transaction.

    Hand edits survive the rebuild where they can be matched to exactly one rebuilt row (see
    `_restore_user_edits`); `ImportResult.restored_edits` / `unmatched_edits` report how many were
    kept and how many had to be dropped because the match was missing or ambiguous. Field
    corrections (date, amount, description) are carried over the same way; the correction is
    dropped when the new parse already equals the corrected value. With ``discard_edits=True``
    nothing is restored (the rebuilt rows are plain parses) and
    `ImportResult.discarded_edits` counts the edited rows that were thrown away.

    A re-import that would leave a statement which has rows with none is refused
    (`EmptyReparseError`, nothing changed) unless ``allow_empty`` says that is intended.
    `ImportResult.previous_txn_count` is the number of rows the statement had before.

    Raises:
        PfaError: the statement was not found, has no account yet (never confirmed), or
            has no PDF copy on disk to re-parse from.
        EmptyReparseError: the new parse found no transactions and the statement had some.
        ValueError: neither or both of `parser_id`/`layout_spec_id` were given.
    """
    statement = session.get(Statement, statement_id)
    if statement is None:
        raise PfaError(f"no statement with id={statement_id}")
    if statement.account_id is None:
        raise PfaError(f"statement {statement_id} has never been confirmed")
    if statement.stored_path is None:
        raise PfaError(
            f"statement {statement_id} has no PDF copy on disk to reparse from "
            "([privacy] store_pdf_copies was disabled at import time)"
        )
    if (parser_id is None) == (layout_spec_id is None):
        raise ValueError("exactly one of parser_id or layout_spec_id must be given")

    account = session.get(Account, statement.account_id)
    if account is None:  # pragma: no cover - defensive; FK integrity guarantees this
        raise PfaError(f"account {statement.account_id} not found")

    # The recorded path is relative to the documents directory; kept_pdf falls back to finding
    # the file by content when the user moved or renamed it.
    pdf_path = docs.kept_pdf(statement.stored_path, statement.file_sha256)
    if pdf_path is None:
        raise PfaError(_pdf_gone_message(statement))
    full_doc = extract(pdf_path)
    # An already-parsed statement must be left untouched by a failed re-import: neither the page
    # selection nor status/error_detail is written until the new parse has succeeded (the error
    # is reported on the job only).
    new_selection = (
        dump_pages(validate_pages(pages, full_doc.page_count)) if pages is not None else None
    )
    doc = full_doc.select_pages(
        load_pages(new_selection if new_selection is not None else statement.selected_pages)
    )
    parser = _resolve_parser(
        session, parser_id=parser_id, layout_spec_id=layout_spec_id, user_id=account.user_id
    )

    parsed = parser.parse(doc)  # UnsupportedLayoutError/ParserError propagate; nothing persisted
    previous_txn_count = (
        session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.statement_id == statement.id)
        )
        or 0
    )
    if not parsed.transactions and previous_txn_count > 0 and not allow_empty:
        raise EmptyReparseError(
            "The re-import found no transactions, so nothing was changed. "
            f"Your {previous_txn_count} transactions are kept."
        )
    if new_selection is not None:
        statement.selected_pages = new_selection

    # Rebuild: this statement's own rows are cleared first, so the standard dedupe-insert path
    # below only ever collides with *other* statements' rows on the same account (§3.5). A row the
    # user edited by hand is remembered first and restored onto its rebuilt twin afterwards.
    kept_edits = _user_edits_of_statement(session, statement.id)
    discarded = 0
    if discard_edits:
        discarded = len(kept_edits)
        kept_edits = []
    session.execute(delete(Transaction).where(Transaction.statement_id == statement.id))
    session.flush()

    result = _persist_parsed_statement(
        session,
        statement=statement,
        parsed=parsed,
        user_id=account.user_id,
        issuer_id=account.issuer_id,
        parser=parser,
        layout_spec_id=layout_spec_id,
        remember=False,  # reparse is an explicit override; it never rewrites the remembered spec
        commit=False,  # rebuild + edit restore are one transaction, committed by the caller
    )
    restored, unmatched = _restore_user_edits(session, statement.id, account.id, kept_edits)
    if unmatched:
        logger.warning(
            "reparse of statement %s dropped %d hand edit(s) it could not match to a rebuilt row",
            statement.id,
            unmatched,
        )
    return replace(
        result,
        restored_edits=restored,
        unmatched_edits=unmatched,
        discarded_edits=discarded,
        previous_txn_count=previous_txn_count,
    )


def _pdf_gone_message(statement: Statement) -> str:
    """Why a kept PDF cannot be found: moved out or deleted, or replaced in place by a file with
    different content (the folder scan then imports that file as a new statement)."""
    stored = docs.resolve(statement.stored_path) if statement.stored_path else None
    if stored is not None and stored.is_file() and sha256_file(stored) != statement.file_sha256:
        return (
            f"The file at {statement.stored_path} now holds a different PDF; "
            "this statement's original is gone."
        )
    return (
        f"the PDF of statement {statement.id} was not found in the documents folder "
        "(it was moved out or deleted); put it back to reparse"
    )


@dataclass(frozen=True, slots=True)
class _UserEdit:
    """A hand edit of a transaction as it must survive the transaction being rebuilt or merged
    into a duplicate: a one-time category/kind edit (`locked`, `classified_by='user'`) or a
    free-text note on a row that is otherwise automatic. The identity fields let a rebuilt row be
    found again even if its `dedupe_hash` changed."""

    category_id: int | None
    subcategory_id: int | None
    confidence: float | None
    needs_review: bool
    kind: str
    is_spend: bool
    merchant_canonical: str | None
    notes: str | None
    locked: bool = True
    #: Field corrections (the corrected values; None = not corrected). The identity fields below
    #: always hold the *imported* values, so a rebuilt row is found by what the parser produced.
    fix_posted_date: str | None = None
    fix_amount_minor: int | None = None
    fix_description_clean: str | None = None
    # Identity (used by `reparse` only).
    dedupe_hash: str = ""
    posted_date: str = ""
    amount_minor: int = 0
    description_raw: str = ""
    description_clean: str = ""
    merchant_key: str = ""
    #: How many of the statement's rows shared this row's (date, amount, raw description) before
    #: the rebuild: twins are told apart only by their position, which a rebuild can shift.
    twin_count: int = 1


def _user_edit_of(txn: Transaction) -> _UserEdit:
    return _UserEdit(
        category_id=txn.category_id,
        subcategory_id=txn.subcategory_id,
        confidence=txn.confidence,
        needs_review=txn.needs_review,
        kind=txn.kind,
        is_spend=txn.is_spend,
        merchant_canonical=txn.merchant_canonical,
        notes=txn.notes,
        locked=txn.classified_by == USER_CLASSIFIED,
        fix_posted_date=txn.posted_date if txn.orig_posted_date is not None else None,
        fix_amount_minor=txn.amount_minor if txn.orig_amount_minor is not None else None,
        fix_description_clean=(
            txn.description_clean if txn.orig_description_clean is not None else None
        ),
        dedupe_hash=txn.dedupe_hash,
        posted_date=_imported(txn.orig_posted_date, txn.posted_date),
        amount_minor=_imported(txn.orig_amount_minor, txn.amount_minor),
        description_raw=txn.description_raw,
        description_clean=_imported(txn.orig_description_clean, txn.description_clean),
        merchant_key=txn.merchant_key,
    )


def _imported[T](original: T | None, live: T) -> T:
    """The value the statement was imported with: `orig_X` when the user corrected the field."""
    return live if original is None else original


def _has_corrections(txn: Transaction) -> bool:
    return (
        txn.orig_posted_date is not None
        or txn.orig_amount_minor is not None
        or txn.orig_description_clean is not None
    )


def _apply_user_edit(txn: Transaction, edit: _UserEdit) -> None:
    if edit.locked:
        txn.category_id = edit.category_id
        txn.subcategory_id = edit.subcategory_id
        txn.confidence = edit.confidence
        txn.needs_review = edit.needs_review
        txn.kind = edit.kind
        txn.is_spend = edit.is_spend
        txn.merchant_canonical = edit.merchant_canonical
        txn.classified_by = USER_CLASSIFIED
    if edit.notes is not None:
        txn.notes = edit.notes
    # Corrections: re-applied onto the freshly parsed row, which keeps the newly parsed value as
    # the "imported" one; a correction the new parse already satisfies is simply not needed.
    # `dedupe_hash` is never touched. `orig_X` is only ever set when it is None, so the true
    # imported value is never overwritten. An amount fix moves the statement's parsed total.
    if edit.fix_posted_date is not None and edit.fix_posted_date != txn.posted_date:
        if txn.orig_posted_date is None:
            txn.orig_posted_date = txn.posted_date
        txn.posted_date = edit.fix_posted_date
    if (
        edit.fix_description_clean is not None
        and edit.fix_description_clean != txn.description_clean
    ):
        if txn.orig_description_clean is None:
            txn.orig_description_clean = txn.description_clean
        txn.description_clean = edit.fix_description_clean
    if edit.fix_amount_minor is not None and edit.fix_amount_minor != txn.amount_minor:
        statement = txn.statement
        if statement.parsed_total_minor is not None:
            statement.parsed_total_minor += edit.fix_amount_minor - txn.amount_minor
        if txn.orig_amount_minor is None:
            txn.orig_amount_minor = txn.amount_minor
        txn.amount_minor = edit.fix_amount_minor
    # A correction that lands back on the imported value is no correction any more.
    if txn.orig_posted_date is not None and txn.posted_date == txn.orig_posted_date:
        txn.orig_posted_date = None
    if (
        txn.orig_description_clean is not None
        and txn.description_clean == txn.orig_description_clean
    ):
        txn.orig_description_clean = None
    if txn.orig_amount_minor is not None and txn.amount_minor == txn.orig_amount_minor:
        txn.orig_amount_minor = None
    txn.updated_at = _utcnow_iso()


#: Machine-written `notes` (transfer pairing, see `_pair_transfers`) are rebuilt with the rows;
#: only text the user typed is a hand edit worth carrying over.
_MACHINE_NOTES_PREFIX = '{"transfer_pair_id"'


def _user_edits_of_statement(session: Session, statement_id: int) -> list[_UserEdit]:
    """The statement's hand-edited rows: those marked `classified_by='user'`, rows that only
    carry a note the user typed, and rows with a field correction. Each remembers how many twins (same date, amount and raw
    description) it had, for `_restore_user_edits`' ambiguity check."""
    rows = list(
        session.execute(
            select(Transaction).where(Transaction.statement_id == statement_id)
        ).scalars()
    )

    def twin_key(txn: Transaction) -> tuple[str, int, str]:
        return (
            _imported(txn.orig_posted_date, txn.posted_date),
            _imported(txn.orig_amount_minor, txn.amount_minor),
            txn.description_raw,
        )

    twins: dict[tuple[str, int, str], int] = {}
    for txn in rows:
        twins[twin_key(txn)] = twins.get(twin_key(txn), 0) + 1
    edits: list[_UserEdit] = []
    for txn in sorted(rows, key=lambda t: t.id):
        is_locked = txn.classified_by == USER_CLASSIFIED
        has_user_note = txn.notes is not None and not txn.notes.startswith(_MACHINE_NOTES_PREFIX)
        if not is_locked and not has_user_note and not _has_corrections(txn):
            continue
        edit = _user_edit_of(txn)
        if not has_user_note:
            edit = replace(edit, notes=None)
        edits.append(replace(edit, twin_count=twins[twin_key(txn)]))
    return edits


def _restore_user_edits(
    session: Session, statement_id: int, account_id: int, edits: list[_UserEdit]
) -> tuple[int, int]:
    """Put each remembered hand edit back on a rebuilt row, never guessing between candidates.
    Returns ``(restored, unmatched)``; an unmatched edit is dropped (its old row is gone).

    Matching, per edit:

    1. **Same `dedupe_hash`** on the account (a rebuilt row of the same transaction hashes the
       same). Twins (same date, amount and raw description) are told apart only by their
       position, so if the rebuild produced a different number of twins than there were before,
       the match is ambiguous and the edit is left unmatched rather than attached to the wrong one.
    2. Otherwise (the parser changed its description text, so the hash differs) the **same
       account, posted date, amount and merchant** (`merchant_key`, or the cleaned description
       when the key is empty) among the rebuilt rows of this statement, but only if exactly one
       edit and exactly one still-unclaimed rebuilt row share that identity.

    Runs right after the rebuild, before anything can classify the new rows (which are then
    skipped as hand-edited). Flushes, does not commit: the caller owns the transaction, as
    `reparse` documents.
    """
    if not edits:
        return 0, 0
    rebuilt = list(
        session.execute(
            select(Transaction).where(Transaction.statement_id == statement_id)
        ).scalars()
    )
    new_twins: dict[tuple[str, int, str], int] = {}
    for txn in rebuilt:
        key = (txn.posted_date, txn.amount_minor, txn.description_raw)
        new_twins[key] = new_twins.get(key, 0) + 1

    by_hash = {
        txn.dedupe_hash: txn
        for txn in session.execute(
            select(Transaction).where(
                Transaction.account_id == account_id,
                Transaction.dedupe_hash.in_([e.dedupe_hash for e in edits]),
            )
        ).scalars()
    }
    claimed: set[int] = set()
    pending: list[_UserEdit] = []
    restored = 0
    for edit in edits:
        match = by_hash.get(edit.dedupe_hash)
        if match is None:
            pending.append(edit)
            continue
        twin_key = (edit.posted_date, edit.amount_minor, edit.description_raw)
        in_statement = match.statement_id == statement_id
        if (in_statement and new_twins.get(twin_key, 0) != edit.twin_count) or (
            match.id in claimed
        ):
            continue  # ambiguous twin: unmatched
        if match.classified_by == USER_CLASSIFIED and edit.locked:
            continue  # the row already carries an edit of its own (another statement's twin)
        claimed.add(match.id)
        _apply_user_edit(match, edit)
        restored += 1

    def identity(txn: Transaction | _UserEdit) -> tuple[str, int, str]:
        who = txn.merchant_key or txn.description_clean
        return (txn.posted_date, txn.amount_minor, who)

    edits_by_identity: dict[tuple[str, int, str], list[_UserEdit]] = {}
    for edit in pending:
        edits_by_identity.setdefault(identity(edit), []).append(edit)
    rows_by_identity: dict[tuple[str, int, str], list[Transaction]] = {}
    for txn in rebuilt:
        if txn.id not in claimed and txn.classified_by != USER_CLASSIFIED:
            rows_by_identity.setdefault(identity(txn), []).append(txn)
    for key, group in edits_by_identity.items():
        candidates = rows_by_identity.get(key, [])
        if len(group) == 1 and len(candidates) == 1:
            claimed.add(candidates[0].id)
            _apply_user_edit(candidates[0], group[0])
            restored += 1

    session.flush()
    return restored, len(edits) - restored


def reassign(
    session: Session, statement_id: int, *, user_id: int, account_id: int
) -> tuple[int, int]:
    """Move a statement (and its transactions) to a different account/user, recomputing
    `dedupe_hash` for every row (D4, §3.5).

    A row whose recomputed hash collides with one already on the target account is a true
    duplicate and is deleted rather than moved. Does not commit (per §3.12a); the caller controls
    the transaction.

    Returns:
        ``(rows_moved, duplicates_deleted)``.

    Raises:
        PfaError: the statement was not found.
    """
    statement = session.get(Statement, statement_id)
    if statement is None:
        raise PfaError(f"no statement with id={statement_id}")

    # Recompute occurrence_index fresh, in the same (id) order the rows were originally inserted
    # in, per §3.5 — the content has not changed, only the account they belong to.
    transactions = list(
        session.execute(
            select(Transaction)
            .where(Transaction.statement_id == statement_id)
            .order_by(Transaction.id)
        )
        .scalars()
        .all()
    )
    keys = [
        DedupeKey(
            account_id=account_id,
            # The *imported* values: `dedupe_hash` identifies the parsed transaction, so a later
            # hand correction of date/amount must not change it (nor how twins are counted).
            posted_date=_imported(t.orig_posted_date, t.posted_date),
            amount_minor=_imported(t.orig_amount_minor, t.amount_minor),
            description_raw=t.description_raw,
        )
        for t in transactions
    ]
    occurrence_indices = compute_occurrence_indices(keys)

    rows_moved = 0
    duplicates_deleted = 0
    for txn, key, occurrence_index in zip(transactions, keys, occurrence_indices, strict=True):
        new_hash = compute_dedupe_hash(key, occurrence_index)
        collision = (
            session.execute(
                select(Transaction.id).where(
                    Transaction.account_id == account_id,
                    Transaction.dedupe_hash == new_hash,
                    Transaction.id != txn.id,
                )
            )
            .scalars()
            .first()
        )
        if collision is not None:
            if txn.classified_by == USER_CLASSIFIED or _has_corrections(txn):
                # The surviving twin is the same real transaction: it inherits the user's edit
                # and field corrections. A survivor with its own hand edit keeps its category
                # edit and notes, but still receives this row's field corrections for every
                # field it has not corrected itself (its own correction wins; nothing is lost).
                survivor = session.get(Transaction, collision)
                if survivor is not None:
                    handed = _user_edit_of(txn)
                    if txn.classified_by != USER_CLASSIFIED or survivor.classified_by == (
                        USER_CLASSIFIED
                    ):
                        handed = replace(handed, locked=False, notes=None)
                    if survivor.orig_posted_date is not None:
                        handed = replace(handed, fix_posted_date=None)
                    if survivor.orig_amount_minor is not None:
                        handed = replace(handed, fix_amount_minor=None)
                    if survivor.orig_description_clean is not None:
                        handed = replace(handed, fix_description_clean=None)
                    _apply_user_edit(survivor, handed)  # moves the survivor statement's total
            if txn.orig_amount_minor is not None and statement.parsed_total_minor is not None:
                # The deleted row stays counted in this statement's parsed total at its imported
                # amount; its correction now lives on the survivor (and its statement's total).
                statement.parsed_total_minor -= txn.amount_minor - txn.orig_amount_minor
            session.delete(txn)
            duplicates_deleted += 1
            continue
        txn.account_id = account_id
        txn.user_id = user_id
        txn.dedupe_hash = new_hash
        rows_moved += 1

    statement.account_id = account_id
    statement.user_id = user_id
    session.flush()
    if duplicates_deleted:
        # Deleted duplicates no longer belong to this statement: keep its count truthful.
        statement.txn_count = (
            session.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.statement_id == statement_id)
            )
            or 0
        )
        session.flush()
    return rows_moved, duplicates_deleted


def resolve_account_for_move(session: Session, statement_id: int, *, user_id: int) -> Account:
    """The account of ``user_id`` that matches the statement's current account (same issuer, type,
    mask and currency), created when that person has none yet: where "change person" moves it.
    Flushes (a new account needs its id), does not commit.

    Raises:
        PfaError: the statement was not found or has no account yet.
    """
    statement = session.get(Statement, statement_id)
    if statement is None:
        raise PfaError(f"no statement with id={statement_id}")
    current = session.get(Account, statement.account_id) if statement.account_id else None
    if current is None:
        raise PfaError(f"statement {statement_id} has no account yet")
    issuer = session.get(Issuer, current.issuer_id)
    return _resolve_account(
        session,
        user_id=user_id,
        issuer_id=current.issuer_id,
        account_type=current.account_type,
        mask=current.mask,
        currency=current.currency,
        issuer_name=issuer.name if issuer is not None else "Unknown",
    )


# ------------------------------------------------------------------------------------------------
# Re-proposing and listing extractors for an existing statement
# ------------------------------------------------------------------------------------------------


def _statement_pdf(statement: Statement) -> Path | None:
    """The PDF a not-yet-parsed or parsed statement can be re-read from: its kept copy (found by
    content when moved), else the staged upload."""
    return docs.kept_pdf(statement.stored_path, statement.file_sha256) or docs.staged_pdf(
        statement.file_sha256
    )


def repropose(session: Session, statement_id: int) -> ImportProposal:
    """Re-run issuer matching and extractor scoring for a statement that is not parsed yet
    (`awaiting_extractor`, `unsupported_layout` or `error`), e.g. after the user remembered an
    extractor or created an issuer for an earlier statement of the same batch. Updates the row's
    issuer, extractor and detect score (not the page selection); the status is left as it is.
    Never calls the LLM. Does not commit.

    Raises:
        PfaError: the statement was not found, is already parsed, or its PDF is unavailable.
    """
    statement = session.get(Statement, statement_id)
    if statement is None:
        raise PfaError(f"no statement with id={statement_id}")
    if statement.status not in (
        STATUS_AWAITING_EXTRACTOR,
        STATUS_UNSUPPORTED_LAYOUT,
        STATUS_ERROR,
    ):
        raise PfaError(f"statement {statement_id} is {statement.status}; it cannot be re-proposed")
    path = _statement_pdf(statement)
    if path is None:
        raise PfaError(f"statement {statement_id} has no PDF on disk to re-propose from")
    # A statement whose import failed is re-proposed without the extractor that just failed.
    failed_parser = (
        statement.parser_id
        if statement.status in (STATUS_UNSUPPORTED_LAYOUT, STATUS_ERROR)
        else None
    )
    proposed = _propose_extractor(
        session,
        extract(path),
        load_settings(),
        exclude=frozenset({failed_parser}) if failed_parser else frozenset(),
    )
    # Conditional update: the (slow) extraction above may have raced a confirm that parsed this
    # statement meanwhile; never overwrite a parsed statement's extractor.
    updated = cast(
        "CursorResult[Any]",
        session.execute(
            update(Statement)
            .where(
                Statement.id == statement_id,
                Statement.status.in_(
                    (STATUS_AWAITING_EXTRACTOR, STATUS_UNSUPPORTED_LAYOUT, STATUS_ERROR)
                ),
            )
            .values(
                issuer_id=proposed.issuer_id,
                layout_spec_id=proposed.layout_spec_id,
                parser_id=proposed.parser_id,
                parser_version=proposed.parser_version,
                detect_score=proposed.detect_score,
            )
            .execution_options(synchronize_session=False)
        ),
    )
    if updated.rowcount == 0:
        session.expire(statement)
        raise PfaError(f"statement {statement_id} is no longer pending; it cannot be re-proposed")
    session.expire(statement)
    return ImportProposal(
        statement_id=statement.id,
        status=statement.status,
        issuer_id=proposed.issuer_id,
        parser_id=proposed.parser_id,
        layout_spec_id=proposed.layout_spec_id,
        detect_score=proposed.detect_score,
        confident=proposed.confident and statement.status == STATUS_AWAITING_EXTRACTOR,
        remembered=proposed.remembered,
    )


@dataclass(frozen=True)
class ExtractorCandidate:
    """One extractor a statement could be (re-)imported with."""

    kind: str  # 'parser' | 'spec' | 'llm'
    parser_id: str | None  # the built-in parser id, or 'llm'; None for a spec
    layout_spec_id: int | None
    label: str
    score: float | None  # `detect()` on the statement's selected pages; None for the LLM
    recorded: bool  # the extractor the statement was imported with
    remembered: bool  # the issuer's remembered choice


def _parser_label(parser_id: str) -> str:
    """``layout_b_credit`` -> ``Layout B credit``."""
    words = parser_id.split("_")
    return " ".join(
        w.upper() if len(w) == 1 else (w.capitalize() if i == 0 else w) for i, w in enumerate(words)
    )


def extractor_candidates(session: Session, statement_id: int) -> list[ExtractorCandidate]:
    """Every extractor that could re-import this statement, scored against its saved page
    selection: each built-in parser (and the generic table parser), every *approved* layout spec
    of the statement's issuer (every version and account type), and the LLM when configured.
    Recorded one first, then best score first. Needs the PDF; makes no LLM call.

    Raises:
        PfaError: the statement was not found or its PDF is unavailable.
    """
    statement = session.get(Statement, statement_id)
    if statement is None:
        raise PfaError(f"no statement with id={statement_id}")
    path = _statement_pdf(statement)
    if path is None:
        raise PfaError(f"statement {statement_id} has no PDF on disk to score extractors on")
    doc = extract(path).select_pages(load_pages(statement.selected_pages))
    issuer = session.get(Issuer, statement.issuer_id) if statement.issuer_id is not None else None

    out: list[ExtractorCandidate] = []
    for parser in (*discover_all_parsers(), generic_table):
        out.append(
            ExtractorCandidate(
                kind="parser",
                parser_id=parser.id,
                layout_spec_id=None,
                label=_parser_label(parser.id),
                score=_detect_score(parser, doc),
                recorded=statement.layout_spec_id is None and statement.parser_id == parser.id,
                remembered=issuer is not None
                and issuer.default_spec_id is None
                and issuer.default_parser_id == parser.id,
            )
        )
    if issuer is not None:
        rows = session.execute(
            select(LayoutSpec)
            .where(LayoutSpec.issuer_id == issuer.id, LayoutSpec.approved.is_(True))
            .order_by(LayoutSpec.name, LayoutSpec.version.desc())
        ).scalars()
        for row in rows:
            try:
                spec_score: float | None = _detect_score(
                    cast(StatementParser, build_parser_for_row(row)), doc
                )
            except Exception:  # a spec that no longer builds is listed, just unscored
                logger.warning("layout spec %s could not be built", row.id, exc_info=True)
                spec_score = None
            out.append(
                ExtractorCandidate(
                    kind="spec",
                    parser_id=None,
                    layout_spec_id=row.id,
                    label=f"{row.name} (v{row.version})",
                    score=spec_score,
                    recorded=statement.layout_spec_id == row.id,
                    remembered=issuer.default_spec_id == row.id,
                )
            )
    if load_settings().llm.is_configured:
        out.append(
            ExtractorCandidate(
                kind="llm",
                parser_id="llm",
                layout_spec_id=None,
                label="LLM",
                score=None,
                recorded=statement.layout_spec_id is None and statement.parser_id == "llm",
                remembered=issuer is not None
                and issuer.default_spec_id is None
                and issuer.default_parser_id == "llm",
            )
        )
    out.sort(key=lambda c: (not c.recorded, -(c.score if c.score is not None else -1.0), c.label))
    return out
