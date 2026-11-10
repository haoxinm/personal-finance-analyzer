"""Single-phase import of a tax-form PDF into the `tax_*` tables (plan 4a).

`import_tax_document` is deterministic and synchronous: extract the text layer, pick a tax parser
(`registry.select_tax_parser`), parse, and persist the document, its forms and their income/tax
items in one transaction. No LLM, no network, no job.

Privacy (plan 3.4): a tax PDF holds full SSNs/EINs. This module therefore

- extracts with ``write_cache=False`` so `[privacy] store_extract_cache` can never dump the text;
- keeps the PDF only when `[privacy] store_tax_pdf_copies` is on (default off). When off, the
  hash-named upload under `statements_dir()` is deleted once the outcome is recorded (the caller's
  own original file is never touched);
- stores only generic error text and logs only ids, counts, form types and parser id/version
  (for an unexpected failure, only the exception class name);
- scrubs the display name (`tin.scrub_display_name`): no SSN shape and no 6+ digit run is stored.

Idempotency: (`user_id`, `file_sha256`) is UNIQUE on `tax_documents` (re-importing the same file for
the same user is a no-op, status ``duplicate``; another user may import the same PDF, and an existing
row's owner is never changed); `tax_forms` additionally dedupe on (user_id, tax_year, fingerprint), so the same
W-2 delivered in two different PDFs is stored once and reported as a skipped duplicate form.

Forms with a *strong* identity (at least one amount AND a payer TIN or account last four) are
deduped across files. A *weak* form that names its payer is deduped on the normalized name plus
identical amounts (a different name never collides). A weak form with no payer name at all gets
the file hash and its position in the file mixed into its fingerprint, so it is never dropped as a
duplicate of another file's form; a soft warning is added when it has the same amounts as a
nameless form of another document.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from personal_finance_analyzer.config import load_settings
from personal_finance_analyzer.core.errors import (
    EgressViolation,
    ParserError,
    UnsupportedLayoutError,
)
from personal_finance_analyzer.core.logging import get_logger
from personal_finance_analyzer.core.paths import ensure_home, kept_pdf, statements_dir
from personal_finance_analyzer.core.tax_types import (
    FORM_ROLE,
    ParsedIncomeItem,
    ParsedTaxForm,
    ParsedTaxItem,
    TaxFallbackExtractor,
)
from personal_finance_analyzer.db.models import IncomeItem, TaxDocument, TaxForm, TaxItem
from personal_finance_analyzer.db.pii_terms import user_pii_terms
from personal_finance_analyzer.ingest.extract import extract, sha256_file
from personal_finance_analyzer.ingest.pipeline import NO_TEXT_LAYER_MESSAGE
from personal_finance_analyzer.ingest.tax import staging
from personal_finance_analyzer.ingest.tax.common import parsing_with_pii_terms
from personal_finance_analyzer.ingest.tax.fallback import apply_fallback
from personal_finance_analyzer.ingest.tax.orchestrator import ParseOutcome, parse_document
from personal_finance_analyzer.ingest.tax.tin import has_long_digit_run, scrub, scrub_display_name

logger = get_logger("ingest.tax.pipeline")

STATUS_PARSED = "parsed"
STATUS_DUPLICATE = "duplicate"
STATUS_ERROR = "error"
STATUS_UNSUPPORTED = "unsupported"

UNSUPPORTED_MESSAGE = "This PDF doesn't look like a supported tax form (W-2, 1099, 1040)."
GENERIC_ERROR_MESSAGE = "This tax form could not be read."


@dataclass(frozen=True)
class TaxImportResult:
    document_id: int
    status: str  # 'parsed' | 'duplicate' | 'error' | 'unsupported'
    tax_year: int | None
    form_types: tuple[str, ...]
    inserted_forms: int
    skipped_duplicate_forms: int
    income_item_count: int
    tax_item_count: int
    warnings: tuple[str, ...]
    error: str | None  # generic message, never document text
    #: ``(document_id, original_name, form_type)`` of the earlier forms that caused a skip.
    skipped_from: tuple[tuple[int, str, str], ...] = ()
    #: True when the opt-in LLM fallback sent at least one request for this file.
    llm_used: bool = False
    #: Number of newly stored forms that were read entirely by the LLM fallback.
    llm_form_count: int = 0


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _staged_path(file_sha256: str) -> Path:
    return statements_dir() / f"{file_sha256}.pdf"


def _is_staged_upload(pdf_path: Path, file_sha256: str) -> bool:
    try:
        return pdf_path.resolve() == _staged_path(file_sha256).resolve()
    except OSError:
        return False


def _keep_or_unstage(pdf_path: Path, file_sha256: str, *, keep: bool) -> str | None:
    """Apply `[privacy] store_tax_pdf_copies`: return `stored_path` (None when not kept)."""
    staged = _staged_path(file_sha256)
    if keep:
        ensure_home()
        if not staged.exists():
            staged.write_bytes(pdf_path.read_bytes())
        return str(staged)
    if _is_staged_upload(pdf_path, file_sha256):
        staged.unlink(missing_ok=True)
    return None


def _form_fingerprint(
    user_id: int, tax_year: int, form: ParsedTaxForm, *, doc_scope: str | None = None
) -> str:
    """Stable identity of a form's content (D9). Hashed, so nothing readable is stored.

    With `doc_scope` (a file hash and form position) the fingerprint is unique to that spot in
    that file; without it, it is the content-only identity shared across files.
    """
    payer_key = " ".join((form.payer_name or "").lower().split())
    income = sorted(f"{i.category}:{i.amount_minor}:{int(i.is_memo)}" for i in form.income)
    taxes = sorted(f"{t.category}:{t.amount_minor}:{t.jurisdiction or ''}" for t in form.taxes)
    parts = [
        str(user_id),
        str(tax_year),
        form.form_type,
        payer_key,
        form.payer_tin_last4 or "",
        form.account_last4 or "",
        ",".join(income),
        ",".join(taxes),
    ]
    if doc_scope is not None:
        parts += ["doc", doc_scope]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _form_identity(
    user_id: int,
    tax_year: int,
    form: ParsedTaxForm,
    *,
    file_sha256: str,
    ordinal: int,
    repeat: int = 0,
) -> tuple[str, Literal["strong", "weak"]]:
    """``(fingerprint, strength)``. Strong = at least one item AND a payer TIN or account last
    four: the content fingerprint, shared across files (the same form delivered twice dedupes).
    Weak = anything else. A weak form that still names its payer keeps the content fingerprint
    (same normalized payer name and identical items = the same form delivered again; a different
    name never collides). A weak form with no payer name has nothing to tell it from another
    provider's: the fingerprint also includes the file hash and the form's position, so it is
    always kept. ``repeat`` counts identical weak forms earlier in the same file: they are real
    separate forms (two identical W-2s), numbered so they stay while the whole set still dedupes
    when the file is delivered again."""
    strong = bool(form.income or form.taxes) and bool(form.payer_tin_last4 or form.account_last4)
    if strong:
        return _form_fingerprint(user_id, tax_year, form), "strong"
    if " ".join((form.payer_name or "").split()):
        content = _form_fingerprint(user_id, tax_year, form)
        if repeat:
            content = hashlib.sha256(f"{content}|repeat|{repeat}".encode()).hexdigest()
        return content, "weak"
    return (
        _form_fingerprint(user_id, tax_year, form, doc_scope=f"{file_sha256}|{ordinal}"),
        "weak",
    )


def fingerprint_from_rows(
    user_id: int,
    form: TaxForm,
    *,
    file_sha256: str,
    ordinal: int,
    repeat: int = 0,
) -> str:
    """The fingerprint an import for `user_id` would give the stored `form` (pure; reads only the
    form row and its income / tax item rows, exactly the inputs `_form_identity` uses).

    Used to move a form to another user: the fingerprint hashes the owner, so it must be
    re-derived for the new owner, both to detect a clash and to keep later imports deduping.
    `ordinal` / `repeat` are the form's position in its file and the count of identical forms
    before it, as `store_forms` computes them."""
    parsed = ParsedTaxForm(
        form_type=form.form_type,  # type: ignore[arg-type]
        tax_year=form.tax_year,
        payer_name=form.payer_name,
        payer_tin_last4=form.payer_tin_last4,
        account_last4=form.account_last4,
        is_corrected=form.is_corrected,
        filing_status=form.filing_status,
        agi_minor=form.agi_minor,
        taxable_income_minor=form.taxable_income_minor,
        income=tuple(
            ParsedIncomeItem(
                category=i.category,  # type: ignore[arg-type]
                amount_minor=i.amount_minor,
                source_form=i.source_form,
                box=i.box,
                label=i.label,
                is_memo=i.is_memo,
            )
            for i in form.income_items
        ),
        taxes=tuple(
            ParsedTaxItem(
                category=t.category,  # type: ignore[arg-type]
                amount_minor=t.amount_minor,
                source_form=t.source_form,
                box=t.box,
                label=t.label,
                jurisdiction=t.jurisdiction,
            )
            for t in form.tax_items
        ),
    )
    return _form_identity(
        user_id,
        form.tax_year,
        parsed,
        file_sha256=file_sha256,
        ordinal=ordinal,
        repeat=repeat,
    )[0]


def _skip_warning(skipped_from: list[tuple[int, str, str]], own_document_id: int) -> str | None:
    """The warning for forms skipped as duplicates, naming the earlier file(s)."""
    if not skipped_from:
        return None
    other = [(d, n) for d, n, _ in skipped_from if d != own_document_id]
    same = len(skipped_from) - len(other)
    parts: list[str] = []
    if other:
        names = list(dict.fromkeys(n for _, n in other))
        shown = ", ".join(f'"{n}"' for n in names[:3]) + (", ..." if len(names) > 3 else "")
        parts.append(
            f"{len(other)} form(s) already imported from {shown} were skipped "
            "(same payer or account identifier and identical amounts)."
        )
    if same:
        parts.append(f"{same} identical form(s) within this file were skipped.")
    return " ".join(parts)


def _amount_multiset(session: Session, form_id: int) -> Counter[tuple[str, int]]:
    rows = [
        (c, a)
        for c, a in session.execute(
            select(IncomeItem.category, IncomeItem.amount_minor).where(
                IncomeItem.form_id == form_id
            )
        )
    ]
    rows += [
        (c, a)
        for c, a in session.execute(
            select(TaxItem.category, TaxItem.amount_minor).where(TaxItem.form_id == form_id)
        )
    ]
    return Counter(rows)


def _lookalike_warning(
    session: Session, doc_id: int, user_id: int, tax_year: int, form_id: int, form: ParsedTaxForm
) -> str | None:
    """A soft warning when a kept form without any identifier has the same amounts as a form
    without a payer name or identifier in another document of the same user and year (only such
    forms are kept despite looking identical); `None` otherwise."""
    if form.payer_name:
        return None
    mine = _amount_multiset(session, form_id)
    candidates = session.execute(
        select(TaxForm.id, TaxDocument.original_name)
        .join(TaxDocument, TaxDocument.id == TaxForm.document_id)
        .where(
            TaxForm.user_id == user_id,
            TaxForm.tax_year == tax_year,
            TaxForm.form_type == form.form_type,
            func.coalesce(TaxForm.payer_name, "") == "",
            TaxForm.payer_tin_last4.is_(None),
            TaxForm.account_last4.is_(None),
            TaxForm.document_id != doc_id,
        )
    ).all()
    for other_id, name in candidates:
        if _amount_multiset(session, other_id) == mine:
            return (
                f'Looks identical to a form in "{name}" (same amounts), but no payer name or '
                "identifier could be read so it was kept. Delete one if it is a duplicate."
            )
    return None


def _safe_message(exc: Exception) -> str:
    """A parser's message if it is clean (no TIN shape, no long digit run), else a fixed one."""
    text = scrub(str(exc)).strip()
    if not text or has_long_digit_run(text) or len(text) > 300:
        return GENERIC_ERROR_MESSAGE
    return text


def _existing_summary(session: Session, doc: TaxDocument) -> TaxImportResult:
    forms = session.scalars(select(TaxForm).where(TaxForm.document_id == doc.id)).all()
    form_ids = [f.id for f in forms]
    income_n = tax_n = 0
    if form_ids:
        income_n = len(
            session.scalars(select(IncomeItem.id).where(IncomeItem.form_id.in_(form_ids))).all()
        )
        tax_n = len(session.scalars(select(TaxItem.id).where(TaxItem.form_id.in_(form_ids))).all())
    warnings: Sequence[str] = json.loads(doc.warnings) if doc.warnings else []
    return TaxImportResult(
        document_id=doc.id,
        status=STATUS_DUPLICATE,
        tax_year=doc.tax_year,
        form_types=tuple(f.form_type for f in forms),
        inserted_forms=0,
        skipped_duplicate_forms=0,
        income_item_count=income_n,
        tax_item_count=tax_n,
        warnings=tuple(warnings),
        error=None,
    )


def _no_result_hint(*, tried: bool) -> str:
    """What to do next when a file with a text layer still yielded nothing."""
    settings = load_settings()
    if settings.llm.is_configured and not settings.ingest.tax_llm_fallback:
        return (
            " You can turn on the LLM fallback for tax forms in Settings (LLM), "
            "or add the figures manually."
        )
    if tried:
        return " Even the LLM fallback could not read it; you can add the figures manually."
    return ""


def _failure(
    session: Session,
    doc: TaxDocument,
    *,
    status: str,
    message: str,
    tax_year: int | None,
) -> TaxImportResult:
    doc.status = status
    doc.error_detail = message
    doc.tax_year = tax_year
    doc.warnings = None
    session.commit()
    logger.info("tax import %s: document_id=%s", status, doc.id)
    return TaxImportResult(
        document_id=doc.id,
        status=status,
        tax_year=tax_year,
        form_types=(),
        inserted_forms=0,
        skipped_duplicate_forms=0,
        income_item_count=0,
        tax_item_count=0,
        warnings=(),
        error=message,
    )


def import_tax_document(
    session: Session,
    pdf_path: Path,
    *,
    user_id: int,
    original_name: str,
    tax_year: int | None = None,
    fallback: TaxFallbackExtractor | None = None,
    pii_terms: Sequence[str] | None = None,
) -> TaxImportResult:
    """Import one tax PDF for ``user_id``. Commits; never raises for bad documents.

    Args:
        session: open session on the app database.
        pdf_path: local PDF file.
        user_id: owner of the imported figures.
        original_name: display name (LOCAL ONLY; SSN shapes and 6+ digit runs are masked).
        tax_year: optional user override for forms that print no year.
        fallback: the opt-in LLM fallback (`None` = fully deterministic, nothing leaves the
            machine). It is consulted only for what the parsers could not read, and before any
            database write, so no network call happens inside a write transaction.
        pii_terms: the user's personal-info terms; parsers never take a match for a payer name.
            ``None`` = `[privacy] pii_terms` plus this user's aliases (`db.pii_terms`).

    Returns:
        A `TaxImportResult`. ``status`` is 'parsed', 'duplicate' (same file already parsed for this
        user and holding forms; a no-op), 'unsupported' (not a tax form we know, or no text layer)
        or 'error' (a recognised form that could not be parsed, or an unexpected failure;
        ``error`` is a generic message). An unsupported/error row is
        reused when the same file is imported again.
    """
    file_sha256 = sha256_file(pdf_path)
    settings = load_settings()
    keep = settings.privacy.store_tax_pdf_copies
    if pii_terms is None:
        pii_terms = user_pii_terms(session, settings, user_id)
    original_name = scrub_display_name(original_name)
    try:
        return _import(
            session,
            pdf_path,
            file_sha256,
            keep,
            user_id=user_id,
            original_name=original_name,
            tax_year=tax_year,
            fallback=fallback,
            pii_terms=pii_terms,
        )
    except EgressViolation:
        raise
    except Exception as exc:
        # Import boundary: a parser/extractor bug (anything beyond ParserError) must not leave a
        # staged PDF with full identifiers on disk, half-written rows, or a 500 from the API.
        # Only the exception class is logged (a message could echo document text).
        logger.error("tax import failed unexpectedly: %s", type(exc).__name__)
        session.rollback()
        try:
            return _record_unexpected_failure(
                session, file_sha256, user_id=user_id, original_name=original_name
            )
        finally:
            _unstage(pdf_path, file_sha256)


def _unstage(pdf_path: Path, file_sha256: str) -> None:
    if _is_staged_upload(pdf_path, file_sha256):
        _staged_path(file_sha256).unlink(missing_ok=True)


def _find_document(session: Session, user_id: int, file_sha256: str) -> TaxDocument | None:
    return session.scalar(
        select(TaxDocument).where(
            TaxDocument.user_id == user_id, TaxDocument.file_sha256 == file_sha256
        )
    )


def _record_unexpected_failure(
    session: Session, file_sha256: str, *, user_id: int, original_name: str
) -> TaxImportResult:
    doc = _find_document(session, user_id, file_sha256)
    if doc is None:
        doc = TaxDocument(
            user_id=user_id,
            file_sha256=file_sha256,
            original_name=original_name,
            status=STATUS_ERROR,
            ingested_at=_utcnow_iso(),
        )
        session.add(doc)
    doc.stored_path = None
    session.flush()
    return _failure(session, doc, status=STATUS_ERROR, message=GENERIC_ERROR_MESSAGE, tax_year=None)


@dataclass
class StoredForms:
    """What `store_forms` did with a batch of parsed forms."""

    inserted_forms: int = 0
    skipped: int = 0
    income_n: int = 0
    tax_n: int = 0
    llm_forms: int = 0
    dropped_empty: int = 0
    inserted_types: list[str] = field(default_factory=list)
    skipped_from: list[tuple[int, str, str]] = field(default_factory=list)
    soft_warnings: list[str] = field(default_factory=list)
    kept_forms: list[ParsedTaxForm] = field(default_factory=list)


def store_forms(
    session: Session,
    doc_row: TaxDocument,
    forms: Sequence[ParsedTaxForm],
    *,
    tax_year: int,
    user_id: int,
    file_sha256: str,
    first_ordinal: int = 0,
) -> StoredForms:
    """Insert `forms` under `doc_row` (no commit). A form already stored for this user and year
    (same fingerprint) is skipped and reported; forms without any figure are ignored."""
    inserted_forms = skipped = income_n = tax_n = llm_forms = 0
    inserted_types: list[str] = []
    skipped_from: list[tuple[int, str, str]] = []
    soft_warnings: list[str] = []
    dropped_empty = 0
    kept_forms: list[ParsedTaxForm] = []
    seen_content: Counter[str] = Counter()
    for ordinal, form in enumerate(forms, start=first_ordinal):
        if not (
            form.income
            or form.taxes
            or form.agi_minor is not None
            or form.taxable_income_minor is not None
        ):
            dropped_empty += 1  # parsers no longer emit these; nothing to store or dedupe
            continue
        kept_forms.append(form)
        content_key = _form_fingerprint(user_id, tax_year, form)
        repeat = seen_content[content_key]
        seen_content[content_key] += 1
        fingerprint, strength = _form_identity(
            user_id,
            tax_year,
            form,
            file_sha256=file_sha256,
            ordinal=ordinal,
            repeat=repeat,
        )
        form_id = session.execute(
            sqlite_insert(TaxForm)
            .values(
                document_id=doc_row.id,
                user_id=user_id,
                tax_year=tax_year,
                form_type=form.form_type,
                role=FORM_ROLE[form.form_type],
                payer_name=form.payer_name,
                payer_tin_last4=form.payer_tin_last4,
                account_last4=form.account_last4,
                is_corrected=form.is_corrected,
                filing_status=form.filing_status,
                agi_minor=form.agi_minor,
                taxable_income_minor=form.taxable_income_minor,
                fingerprint=fingerprint,
                extraction_method=form.extraction_method,
                is_user_edited=False,
                created_at=_utcnow_iso(),
            )
            .on_conflict_do_nothing(index_elements=["user_id", "tax_year", "fingerprint"])
            .returning(TaxForm.id)
        ).scalar_one_or_none()
        if form_id is None:
            skipped += 1
            earlier = session.execute(
                select(TaxDocument.id, TaxDocument.original_name)
                .join(TaxForm, TaxForm.document_id == TaxDocument.id)
                .where(
                    TaxForm.user_id == user_id,
                    TaxForm.tax_year == tax_year,
                    TaxForm.fingerprint == fingerprint,
                )
                .limit(1)
            ).first()
            if earlier is not None:
                skipped_from.append((earlier[0], earlier[1], form.form_type))
            continue
        inserted_forms += 1
        llm_forms += form.extraction_method == "llm"
        inserted_types.append(form.form_type)
        if form.income:
            session.execute(
                sqlite_insert(IncomeItem).values(
                    [
                        {
                            "form_id": form_id,
                            "user_id": user_id,
                            "tax_year": tax_year,
                            "category": i.category,
                            "amount_minor": i.amount_minor,
                            "currency": "USD",
                            "source_form": i.source_form,
                            "box": i.box,
                            "label": i.label,
                            "is_memo": i.is_memo,
                            "origin": i.origin,
                        }
                        for i in form.income
                    ]
                )
            )
            income_n += len(form.income)
        if form.taxes:
            session.execute(
                sqlite_insert(TaxItem).values(
                    [
                        {
                            "form_id": form_id,
                            "user_id": user_id,
                            "tax_year": tax_year,
                            "category": t.category,
                            "amount_minor": t.amount_minor,
                            "currency": "USD",
                            "source_form": t.source_form,
                            "box": t.box,
                            "label": t.label,
                            "jurisdiction": t.jurisdiction,
                            "is_estimate": t.is_estimate,
                            "origin": t.origin,
                        }
                        for t in form.taxes
                    ]
                )
            )
            tax_n += len(form.taxes)
        if strength == "weak":
            session.flush()
            note = _lookalike_warning(session, doc_row.id, user_id, tax_year, form_id, form)
            if note is not None:
                soft_warnings.append(note)
    return StoredForms(
        inserted_forms=inserted_forms,
        skipped=skipped,
        income_n=income_n,
        tax_n=tax_n,
        llm_forms=llm_forms,
        dropped_empty=dropped_empty,
        inserted_types=inserted_types,
        skipped_from=skipped_from,
        soft_warnings=soft_warnings,
        kept_forms=kept_forms,
    )


def _import(
    session: Session,
    pdf_path: Path,
    file_sha256: str,
    keep: bool,
    *,
    user_id: int,
    original_name: str,
    tax_year: int | None,
    fallback: TaxFallbackExtractor | None = None,
    pii_terms: Sequence[str] = (),
) -> TaxImportResult:
    doc_row = _find_document(session, user_id, file_sha256)
    if doc_row is not None and doc_row.status == STATUS_PARSED:
        has_forms = session.scalar(
            select(TaxForm.id).where(TaxForm.document_id == doc_row.id).limit(1)
        )
        if has_forms is not None:
            # Re-import of an identical, already-parsed file is a no-op (I9 analogue).
            _keep_or_unstage(pdf_path, file_sha256, keep=keep)
            return _existing_summary(session, doc_row)
        # Parsed but holds no forms (they were all deduped into another document that has since
        # been deleted): the import is re-run so the figures come back.

    try:
        extracted = extract(pdf_path, write_cache=False)
    except ParserError as exc:  # e.g. password protected: message is generic and actionable
        return _record_failure(
            session,
            doc_row,
            pdf_path,
            file_sha256,
            user_id,
            original_name,
            keep,
            status=STATUS_ERROR,
            message=_safe_message(exc),
            tax_year=tax_year,
            page_count=None,
        )

    stored_path = _keep_or_unstage(pdf_path, file_sha256, keep=keep)

    # Parse (and, when opted in, consult the LLM fallback) BEFORE anything is written: a network
    # call must never happen while a database write transaction is open.
    failure: tuple[str, str] | None = None
    outcome: ParseOutcome | None = None
    if not extracted.has_text_layer:
        failure = (STATUS_UNSUPPORTED, NO_TEXT_LAYER_MESSAGE)
    else:
        try:
            with parsing_with_pii_terms(pii_terms):
                outcome = parse_document(extracted, tax_year=tax_year)
        except UnsupportedLayoutError:
            failure = (STATUS_UNSUPPORTED, UNSUPPORTED_MESSAGE)
        except ParserError as exc:
            failure = (STATUS_ERROR, _safe_message(exc))
    run = apply_fallback(extracted, outcome, fallback, tax_year=tax_year)
    parsed = run.document

    if doc_row is None:
        doc_row = TaxDocument(
            user_id=user_id,
            file_sha256=file_sha256,
            original_name=original_name,
            status=STATUS_ERROR,
            ingested_at=_utcnow_iso(),
        )
        session.add(doc_row)
    doc_row.original_name = original_name
    doc_row.stored_path = stored_path
    doc_row.page_count = extracted.page_count
    session.flush()

    if parsed is None:
        status, message = failure or (STATUS_ERROR, GENERIC_ERROR_MESSAGE)
        if extracted.has_text_layer:
            message += _no_result_hint(tried=run.attempted)
        return _failure(session, doc_row, status=status, message=message, tax_year=tax_year)
    if outcome is not None:
        doc_row.parser_id = outcome.parser_id
        doc_row.parser_version = outcome.parser_version
    elif fallback is not None:
        doc_row.parser_id = fallback.id
        doc_row.parser_version = fallback.version

    stored = store_forms(
        session,
        doc_row,
        parsed.forms,
        tax_year=parsed.tax_year,
        user_id=user_id,
        file_sha256=file_sha256,
    )
    inserted_forms, skipped, income_n = stored.inserted_forms, stored.skipped, stored.income_n
    tax_n, llm_forms, dropped_empty = stored.tax_n, stored.llm_forms, stored.dropped_empty
    skipped_from, soft_warnings, kept_forms = (
        stored.skipped_from,
        stored.soft_warnings,
        stored.kept_forms,
    )

    warnings = list(parsed.warnings)
    for form in kept_forms:
        for w in form.warnings:
            if w not in warnings:
                warnings.append(w)
    if dropped_empty:
        warnings.append(f"{dropped_empty} form(s) with no readable amounts were ignored.")
    if skipped:
        note = _skip_warning(skipped_from, doc_row.id)
        warnings.append(
            note
            or f"{skipped} form(s) in this file were already imported from another file "
            "and were skipped."
        )
    for note in dict.fromkeys(soft_warnings):
        warnings.append(note)
    doc_row.status = STATUS_PARSED
    doc_row.error_detail = None
    doc_row.tax_year = parsed.tax_year
    doc_row.warnings = json.dumps(warnings) if warnings else None
    session.commit()
    logger.info(
        "tax import parsed: document_id=%s parser=%s@%s forms=%s inserted=%d skipped=%d "
        "income_items=%d tax_items=%d unparsed_regions=%d llm_requests=%d llm_forms=%d",
        doc_row.id,
        doc_row.parser_id,
        doc_row.parser_version,
        ",".join(f.form_type for f in parsed.forms),
        inserted_forms,
        skipped,
        income_n,
        tax_n,
        len(parsed.unparsed),
        run.requests_sent,
        llm_forms,
    )
    return TaxImportResult(
        document_id=doc_row.id,
        status=STATUS_PARSED,
        tax_year=parsed.tax_year,
        form_types=tuple(f.form_type for f in kept_forms),
        inserted_forms=inserted_forms,
        skipped_duplicate_forms=skipped,
        income_item_count=income_n,
        tax_item_count=tax_n,
        warnings=tuple(warnings),
        error=None,
        skipped_from=tuple(skipped_from),
        llm_used=run.requests_sent > 0,
        llm_form_count=llm_forms,
    )


def _record_failure(
    session: Session,
    doc_row: TaxDocument | None,
    pdf_path: Path,
    file_sha256: str,
    user_id: int,
    original_name: str,
    keep: bool,
    *,
    status: str,
    message: str,
    tax_year: int | None,
    page_count: int | None,
) -> TaxImportResult:
    """Record a failure that happened before extraction produced a document (unreadable PDF)."""
    stored_path = _keep_or_unstage(pdf_path, file_sha256, keep=keep)
    if doc_row is None:
        doc_row = TaxDocument(
            user_id=user_id,
            file_sha256=file_sha256,
            original_name=original_name,
            status=status,
            ingested_at=_utcnow_iso(),
        )
        session.add(doc_row)
    doc_row.stored_path = stored_path
    doc_row.page_count = page_count
    session.flush()
    return _failure(session, doc_row, status=status, message=message, tax_year=tax_year)


def delete_tax_document(session: Session, document_id: int) -> bool:
    """Delete a tax document and (by cascade) its forms and items; remove a kept PDF copy.

    Returns:
        True if a document was deleted, False if ``document_id`` does not exist.
    """
    doc = session.get(TaxDocument, document_id)
    if doc is None:
        return False
    sha = doc.file_sha256
    # The real file, even when the recorded absolute path went stale with a moved data directory.
    kept = kept_pdf(doc.stored_path, sha) if doc.stored_path else None
    session.delete(doc)
    session.commit()
    staging.discard(document_id)
    if kept is not None:
        # Another user's document imported from the same PDF shares the kept copy (it is named by
        # content hash); only remove it when nothing else keeps a copy of that file.
        still_used = session.scalar(
            select(TaxDocument.id)
            .where(TaxDocument.file_sha256 == sha, TaxDocument.stored_path.is_not(None))
            .limit(1)
        )
        if still_used is None:
            kept.unlink(missing_ok=True)
    logger.info("tax document deleted: document_id=%s", document_id)
    return True
