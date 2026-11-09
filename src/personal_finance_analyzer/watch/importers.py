"""Importing one file found in the documents folder: the statement and the tax-form path.

Both go through the same pipelines as an upload (`api.services.gateway`), with the file recorded
*in place* (``in_place=<relative path>``): its location becomes ``stored_path`` and nothing is
copied or renamed. A tax form is recorded this way whatever ``[privacy] store_tax_pdf_copies``
says: that setting decides whether the app *copies* an upload, and this file is already where the
user wants it.

Messages and hints stored here never contain document text.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from personal_finance_analyzer.api.services import crud, gateway
from personal_finance_analyzer.api.services.uploads import sanitize_original_name
from personal_finance_analyzer.config import load_settings
from personal_finance_analyzer.core import documents as docs
from personal_finance_analyzer.core.errors import PfaError, UnsupportedLayoutError
from personal_finance_analyzer.db.models import Statement, TaxDocument, User
from personal_finance_analyzer.ingest.tax.common import TAX_YEAR_UNKNOWN_MESSAGE

logger = logging.getLogger(__name__)


@dataclass
class Context:
    session: Session
    session_factory: sessionmaker[Session]
    path: Path
    rel: str
    sha: str
    user_id: int
    person_name: str
    folder_year: int | None
    dry_run: bool = False


@dataclass
class Result:
    #: imported | known | failed | would_import
    outcome: str
    code: str | None = None
    message: str | None = None
    hint: str | None = None
    statement_id: int | None = None
    tax_document_id: int | None = None
    warnings: list[str] = field(default_factory=list)


def _user_name(session: Session, user_id: int | None) -> str:
    user = session.get(User, user_id) if user_id is not None else None
    return user.name if user is not None else "someone else"


def _live_elsewhere(stored_path: str | None, rel: str) -> bool:
    """Whether ``stored_path`` names an existing file other than ``rel``."""
    if not stored_path or stored_path.casefold() == rel.casefold():
        return False
    if Path(stored_path).is_absolute():
        return False
    found = docs.resolve(stored_path)
    return found is not None and found.is_file()


def _other_person(ctx: Context, owner_id: int, where: str | None) -> Result:
    who = _user_name(ctx.session, owner_id)
    place = f" ({where})" if where else ""
    return Result(
        "failed",
        code="duplicate_other_person",
        message=f"The same file is already imported for {who}{place}.",
        hint=(
            f"Delete this copy, or move it to {who}'s folder. To import it for "
            f"{ctx.person_name} as well, upload it from the Import page."
        ),
    )


# ---------------------------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------------------------


def import_statement(ctx: Context) -> Result:
    session = ctx.session
    row = session.scalar(select(Statement).where(Statement.file_sha256 == ctx.sha))
    if row is not None and row.status == "parsed":
        owner = row.user_id if row.user_id is not None else crud.statement_user_id(session, row)
        if owner is not None and owner != ctx.user_id:
            return _other_person(ctx, owner, row.stored_path)
        if _live_elsewhere(row.stored_path, ctx.rel):
            return Result(
                "known",
                message=f"Same content as {row.stored_path}, already imported.",
                statement_id=row.id,
            )
        if ctx.dry_run:
            return Result("known", message="Already imported; this file would be recorded.")
        row.stored_path = ctx.rel  # imported before without keeping a PDF, or the file was moved
        session.commit()
        return Result(
            "known",
            message="Already imported; this file is now recorded as its PDF.",
            statement_id=row.id,
        )
    if ctx.dry_run:
        return Result("would_import", message="Would be imported as a statement.")

    settings = load_settings()
    try:
        proposal = gateway.propose_import(
            session,
            ctx.path,
            user_id=ctx.user_id,
            original_name=sanitize_original_name(ctx.path.name),
            in_place=ctx.rel,
        )
        session.commit()
    except PfaError as exc:  # a password-protected or damaged PDF
        session.rollback()
        text = str(exc)
        encrypted = "password" in text.lower()
        return Result(
            "failed",
            code="encrypted" if encrypted else "unreadable",
            message=text or "This PDF could not be read.",
            hint=(
                "Remove the password (print to a new PDF) and press Retry."
                if encrypted
                else "Copy the file again, then press Retry."
            ),
        )
    if proposal.status == "duplicate":
        return Result("known", message="Already imported.", statement_id=proposal.statement_id)
    if proposal.status == "no_text_layer":
        return Result(
            "failed",
            code="no_text_layer",
            message="No text in this PDF: it looks like a scan or an image.",
            hint="Download the statement from the bank's website as a digital PDF instead.",
            statement_id=proposal.statement_id,
        )
    if proposal.status != "awaiting_extractor":
        return Result(
            "failed",
            code="parse_error",
            message=f"The statement could not be read (status: {proposal.status}).",
            statement_id=proposal.statement_id,
        )
    if proposal.parser_id == "llm":
        return Result(
            "failed",
            code="needs_extractor",
            message="Statements are set to be read by the LLM, which needs you to choose pages.",
            hint="Open the Import page and upload this file to choose the pages to send.",
            statement_id=proposal.statement_id,
        )
    if not proposal.confident or settings.ingest.always_confirm_extractor:
        why = (
            "Settings ask for every extractor to be confirmed."
            if proposal.confident
            else "The app does not recognise this statement's layout yet."
        )
        return Result(
            "failed",
            code="needs_extractor",
            message=why,
            hint=(
                "Upload this file once from the Import page and choose its extractor; statements "
                "from the same bank are then recognised automatically."
            ),
            statement_id=proposal.statement_id,
        )

    issuer_id = (
        proposal.issuer_id if proposal.issuer_id is not None else crud.unknown_issuer_id(session)
    )
    try:
        result = gateway.confirm_import(
            session,
            proposal.statement_id,
            issuer_id=issuer_id,
            parser_id=proposal.parser_id,
            layout_spec_id=proposal.layout_spec_id,
            remember=False,
            keep_in_place=True,
        )
        session.commit()
    except PfaError as exc:
        session.rollback()
        code = "unsupported_layout" if isinstance(exc, UnsupportedLayoutError) else "parse_error"
        return Result(
            "failed",
            code=code,
            message=str(exc) or "The statement could not be read.",
            hint="Open the Import page and upload this file to try another extractor.",
            statement_id=proposal.statement_id,
        )
    warnings = list(result.warnings)
    if result.layout_drift:
        warnings.append("The statement's layout differs from the previous one; check the totals.")
    if result.transaction_ids:
        warnings.extend(_classify(ctx, result.transaction_ids))
    inserted = f"{result.inserted} transaction(s)"
    skipped = (
        f", {result.skipped_duplicates} duplicate(s) skipped" if result.skipped_duplicates else ""
    )
    return Result(
        "imported",
        message=f"Imported {inserted}{skipped}.",
        statement_id=proposal.statement_id,
        warnings=warnings,
    )


def _classify(ctx: Context, transaction_ids: tuple[int, ...]) -> list[str]:
    """Classify the new transactions exactly as the Import page does (rules first; the LLM only
    when it is enabled in Settings). A failure leaves them unclassified, not the import undone."""
    settings = load_settings()
    try:
        gateway.classify_transactions(
            ctx.session_factory,
            transaction_ids,
            cfg=settings.llm,
            group_id=str(uuid.uuid4()),
            progress_cb=lambda _done, _total, _cost: None,
        )
    except Exception as exc:
        logger.warning("classifying a folder import failed: %s", type(exc).__name__)
        return ["The transactions were imported but could not be categorized; see the Review page."]
    return []


# ---------------------------------------------------------------------------------------------
# Tax forms
# ---------------------------------------------------------------------------------------------


def import_tax(ctx: Context) -> Result:
    session = ctx.session
    rows = session.scalars(select(TaxDocument).where(TaxDocument.file_sha256 == ctx.sha)).all()
    parsed = [r for r in rows if r.status == "parsed"]
    if parsed:
        mine = next((r for r in parsed if r.user_id == ctx.user_id), None)
        if mine is None:
            return _other_person(ctx, parsed[0].user_id, parsed[0].stored_path)
        if _live_elsewhere(mine.stored_path, ctx.rel):
            return Result(
                "known",
                message=f"Same content as {mine.stored_path}, already imported.",
                tax_document_id=mine.id,
            )
        if ctx.dry_run:
            return Result("known", message="Already imported; this file would be recorded.")
        mine.stored_path = ctx.rel
        session.commit()
        return Result(
            "known",
            message="Already imported; this file is now recorded as its PDF.",
            tax_document_id=mine.id,
        )
    if ctx.dry_run:
        return Result("would_import", message="Would be imported as a tax form.")

    from personal_finance_analyzer.api.services.tax_llm import build_tax_fallback

    fallback = build_tax_fallback(session, ctx.user_id)
    name = sanitize_original_name(PurePosixPath(ctx.rel).name)

    def run(year: int | None) -> gateway.TaxImportResult:
        return gateway.import_tax_document(
            session,
            ctx.path,
            user_id=ctx.user_id,
            original_name=name,
            tax_year=year,
            fallback=fallback,
            in_place=ctx.rel,
        )

    result = run(None)  # the form's own year wins; the folder only fills in a year it lacks
    if (
        result.status in ("error", "unsupported")
        and ctx.folder_year is not None
        and TAX_YEAR_UNKNOWN_MESSAGE in (result.error or "")
    ):
        result = run(ctx.folder_year)

    warnings = list(result.warnings)
    if result.status == "parsed":
        if (
            ctx.folder_year is not None
            and result.tax_year is not None
            and result.tax_year != ctx.folder_year
        ):
            warnings.append(
                f"The form is for tax year {result.tax_year}, but it is in the "
                f"{ctx.folder_year} folder. It was imported as {result.tax_year}."
            )
        forms = ", ".join(result.form_types) or "no forms"
        return Result(
            "imported",
            message=f"Imported {forms} for {result.tax_year}.",
            tax_document_id=result.document_id,
            warnings=warnings,
        )
    if result.status == "duplicate":
        return Result("known", message="Already imported.", tax_document_id=result.document_id)
    text = result.error or "This tax form could not be read."
    code = (
        "encrypted"
        if "password" in text.lower()
        else "unsupported_layout"
        if result.status == "unsupported"
        else "parse_error"
    )
    return Result(
        "failed",
        code=code,
        message=text,
        hint=(
            "Remove the password (print to a new PDF) and press Retry."
            if code == "encrypted"
            else "Import it from the Tax page to add the figures by hand."
        ),
        tax_document_id=result.document_id,
        warnings=warnings,
    )
