"""`/api/tax` (income & tax analysis from W-2 / 1099 / 1040 PDFs).

Upload hardening is shared with `/api/statements` (`api/services/uploads.py`). Import is a single
synchronous step (`gateway.import_tax_document`); all tax SQL lives in `analytics/tax_query.py`.

Privacy: responses expose only the last four digits of a taxpayer identifier and never
`stored_path`; `payer_name` is local-only data. Tax data is never sent to an LLM unless the user opts in
(`[ingest] tax_llm_fallback`, see `api/services/tax_llm.py`); then only redacted pages the built-in
parsers could not read are sent. A user can also pick pages of a document (whose PDF copy is kept,
`[privacy] store_tax_pdf_copies`, or was re-selected by the user and is staged temporarily,
`ingest/tax/staging.py`, `POST /tax/documents/{id}/source`), preview the exact redacted text and send just those pages
(`/tax/documents/{id}/llm-preview`, `/llm-fallback`): that click is the consent, so it works
whenever an LLM is configured.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from spend_analyzer.analytics import tax_query
from spend_analyzer.analytics.tax_query import TaxQuery
from spend_analyzer.api import schemas
from spend_analyzer.api.deps import SessionDep, SettingsDep
from spend_analyzer.api.services import crud, gateway, statement_preview, tax_llm, tax_params_update
from spend_analyzer.api.services.uploads import (
    read_pdf_upload,
    sanitize_original_name,
    save_upload,
)
from spend_analyzer.config import Settings
from spend_analyzer.core.errors import SpendAnalyzerError
from spend_analyzer.core.tax_types import (
    INCOME_CATEGORY_LABELS,
    MEMO_INCOME_CATEGORIES,
    TAX_CATEGORY_LABELS,
    TaxCategory,
)
from spend_analyzer.db.models import IncomeItem, TaxDocument, TaxForm, TaxItem
from spend_analyzer.ingest.pages import parse_page_spec
from spend_analyzer.ingest.tax import edit, on_demand, staging
from spend_analyzer.ingest.tax.pipeline import delete_tax_document

router = APIRouter(tags=["tax"])

_MIN_YEAR = 1990
_MAX_YEAR = 2100
#: Labels for the memo keys in `TaxYearSummary.memo_by_category`.
_MEMO_LABELS = {
    "qualified_dividends": INCOME_CATEGORY_LABELS["qualified_dividends"],
    tax_query.TAX_EXEMPT_MEMO_KEY: "Tax-exempt interest (memo)",
}


def _warnings(doc: TaxDocument) -> list[str]:
    if not doc.warnings:
        return []
    loaded = json.loads(doc.warnings)
    return [str(w) for w in loaded] if isinstance(loaded, list) else []


def _origin(value: str) -> Literal["parser", "llm", "manual"]:
    return "llm" if value == "llm" else "manual" if value == "manual" else "parser"


def _method(value: str) -> Literal["parser", "llm", "manual"]:
    return _origin(value)


def _income_item(item: IncomeItem) -> schemas.IncomeItemOut:
    return schemas.IncomeItemOut(
        id=item.id,
        category=schemas.IncomeCategoryEnum(item.category),
        amount_minor=item.amount_minor,
        currency=item.currency,
        source_form=item.source_form,
        box=item.box,
        label=item.label,
        is_memo=item.is_memo,
        origin=_origin(item.origin),
    )


def _tax_item(item: TaxItem) -> schemas.TaxItemOut:
    return schemas.TaxItemOut(
        id=item.id,
        category=schemas.TaxCategoryEnum(item.category),
        amount_minor=item.amount_minor,
        currency=item.currency,
        source_form=item.source_form,
        box=item.box,
        label=item.label,
        jurisdiction=item.jurisdiction,
        is_estimate=item.is_estimate,
        origin=_origin(item.origin),
    )


def _form_fields(form: TaxForm) -> dict[str, object]:
    return {
        "id": form.id,
        "form_type": schemas.TaxFormType(form.form_type),
        "role": form.role,
        "tax_year": form.tax_year,
        "payer_name": form.payer_name,
        "payer_tin_last4": form.payer_tin_last4,
        "account_last4": form.account_last4,
        "filing_status": form.filing_status,
        "agi_minor": form.agi_minor,
        "taxable_income_minor": form.taxable_income_minor,
        "is_corrected": form.is_corrected,
        "extraction_method": _method(form.extraction_method),
        "is_user_edited": form.is_user_edited,
        "needs_review": any(i.origin == "llm" for i in form.income_items)
        or any(t.origin == "llm" for t in form.tax_items),
    }


def _document(doc: TaxDocument) -> schemas.TaxDocument:
    return schemas.TaxDocument(
        id=doc.id,
        user_id=doc.user_id,
        original_name=doc.original_name,
        tax_year=doc.tax_year,
        status=doc.status,
        form_types=[schemas.TaxFormType(f.form_type) for f in doc.forms],
        page_count=doc.page_count,
        warnings=_warnings(doc),
        error_detail=doc.error_detail,
        created_at=doc.ingested_at,
        has_pdf=_stored_pdf(doc) is not None,
        staged_until=staging.expires_at(doc.id),
    )


def _stored_pdf(doc: TaxDocument) -> Path | None:
    """The kept PDF copy (`[privacy] store_tax_pdf_copies`), or `None`."""
    if not doc.stored_path:
        return None
    path = Path(doc.stored_path)
    return path if path.is_file() else None


def _document_detail(doc: TaxDocument) -> schemas.TaxDocumentDetail:
    base = _document(doc)
    forms = [_form_detail(f) for f in doc.forms]
    return schemas.TaxDocumentDetail(**base.model_dump(), forms=forms)


def _check_year(year: int | None, *, name: str = "tax_year") -> None:
    if year is not None and not (_MIN_YEAR <= year <= _MAX_YEAR):
        raise HTTPException(status_code=400, detail=f"invalid {name}: {year}")


def _user_filter(user_id: int | None) -> list[int] | None:
    return [user_id] if user_id is not None else None


@router.post("/tax/documents", response_model=schemas.TaxImportResponse, status_code=201)
async def upload_tax_document(
    file: UploadFile,
    session: SessionDep,
    user_id: int = Form(...),
    tax_year: int | None = Form(None),
) -> schemas.TaxImportResponse:
    """Multipart upload of one tax-form PDF; parsed synchronously. A non-tax or unreadable
    PDF still returns 201 with `status` `unsupported` / `error` and a generic message."""
    data = await read_pdf_upload(file)
    _check_year(tax_year)
    if crud.get_user(session, user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    original_name = sanitize_original_name(file.filename or "tax-form.pdf", "tax-form.pdf")
    pdf_path = save_upload(data)

    def _import() -> gateway.TaxImportResult:
        # The opt-in LLM fallback can take seconds per request, so the whole import runs in a
        # worker thread (the request's session is used by this one thread at a time, and the
        # engine allows cross-thread use). The fallback is built first, outside any write.
        fallback = tax_llm.build_tax_fallback(session, user_id)
        return gateway.import_tax_document(
            session,
            pdf_path,
            user_id=user_id,
            original_name=original_name,
            tax_year=tax_year,
            fallback=fallback,
        )

    result = await run_in_threadpool(_import)
    doc = tax_query.get_tax_document(session, result.document_id)
    if doc is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail="import did not create a document")
    return schemas.TaxImportResponse(
        document=_document(doc),
        status=result.status,
        inserted_forms=result.inserted_forms,
        skipped_duplicate_forms=result.skipped_duplicate_forms,
        skipped_from=[
            schemas.TaxSkippedFrom(document_id=d, original_name=n, form_type=t)
            for d, n, t in result.skipped_from
        ],
        income_item_count=result.income_item_count,
        tax_item_count=result.tax_item_count,
        warnings=list(result.warnings),
        error=result.error,
        llm_used=result.llm_used,
        llm_form_count=result.llm_form_count,
    )


@router.get("/tax/documents", response_model=list[schemas.TaxDocument])
def list_documents(
    session: SessionDep, user_id: int | None = None, tax_year: int | None = None
) -> list[schemas.TaxDocument]:
    _check_year(tax_year)
    docs = tax_query.list_tax_documents(session, user_ids=_user_filter(user_id), tax_year=tax_year)
    return [_document(d) for d in docs]


# --- ownership check ------------------------------------------------------------------------------
#
# Every route that takes a bare id accepts an optional `user_id` query parameter; when given, the
# target must belong to that user or the answer is the same 404 as for a missing row. One dependency
# per id kind, all delegating to `edit.require_owner`.


def _owner_check(
    kind: edit.OwnerKind, session: Session, target_id: int, user_id: int | None
) -> None:
    try:
        edit.require_owner(session, kind, target_id, user_id)
    except edit.EditError as exc:
        raise _raise(exc) from None


def _owns_document(document_id: int, session: SessionDep, user_id: int | None = None) -> None:
    _owner_check("document", session, document_id, user_id)


def _owns_form(form_id: int, session: SessionDep, user_id: int | None = None) -> None:
    _owner_check("form", session, form_id, user_id)


def _owns_income_item(item_id: int, session: SessionDep, user_id: int | None = None) -> None:
    _owner_check("income_item", session, item_id, user_id)


def _owns_tax_item(item_id: int, session: SessionDep, user_id: int | None = None) -> None:
    _owner_check("tax_item", session, item_id, user_id)


_DOC_OWNER = [Depends(_owns_document)]
_FORM_OWNER = [Depends(_owns_form)]
_INCOME_OWNER = [Depends(_owns_income_item)]
_TAX_ITEM_OWNER = [Depends(_owns_tax_item)]


@router.get(
    "/tax/documents/{document_id}",
    response_model=schemas.TaxDocumentDetail,
    dependencies=_DOC_OWNER,
)
def get_document(document_id: int, session: SessionDep) -> schemas.TaxDocumentDetail:
    doc = tax_query.get_tax_document(session, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="tax document not found")
    return _document_detail(doc)


PDF_NEEDED_MESSAGE = (
    "The original file isn't available any more (it isn't kept after import for privacy). "
    "Select the same file again to read its pages."
)


def _document_with_pdf(session: Session, document_id: int) -> tuple[TaxDocument, Path]:
    """The document and its PDF: the kept copy, else the copy the user re-selected (staged
    temporarily). 409 with ``code: pdf_needed`` when neither exists, so the UI asks for the file."""
    doc = tax_query.get_tax_document(session, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="tax document not found")
    pdf = _stored_pdf(doc) or staging.get(doc.id)
    if pdf is None:
        raise HTTPException(
            status_code=409, detail={"code": "pdf_needed", "message": PDF_NEEDED_MESSAGE}
        )
    return doc, pdf


def _page_infos(
    session: Session, doc: TaxDocument, pdf: Path, settings: Settings
) -> list[schemas.TaxPageInfo]:
    terms = tax_llm.pii_terms_for(session, doc.user_id, settings)
    try:
        plan = on_demand.load_plan(pdf, pii_terms=terms, document_year=doc.tax_year)
    except SpendAnalyzerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return [
        schemas.TaxPageInfo(
            page_number=f.page_number,
            char_count=f.char_count,
            suggested=f.suggested,
            excluded_reason=f.excluded_reason,
        )
        for f in plan.facts
    ]


@router.post(
    "/tax/documents/{document_id}/source",
    response_model=schemas.TaxSourceResponse,
    dependencies=_DOC_OWNER,
)
async def provide_document_source(
    document_id: int, file: UploadFile, session: SessionDep, settings: SettingsDep
) -> schemas.TaxSourceResponse:
    """Re-select the original PDF of a document whose copy is not kept. The file must be
    byte-identical to the imported one (sha256); it is held in a private temporary location for
    30 minutes (or until the LLM read, or the document, is gone) so the page tools work. Nothing
    is kept otherwise."""
    data = await read_pdf_upload(file)
    doc = tax_query.get_tax_document(session, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="tax document not found")
    if doc.page_count is None or doc.file_sha256.startswith("manual:"):
        raise HTTPException(status_code=409, detail="This document has no file.")
    if hashlib.sha256(data).hexdigest() != doc.file_sha256:
        raise HTTPException(
            status_code=422, detail=f"This isn't the same file as {doc.original_name}"
        )

    def _stage() -> list[schemas.TaxPageInfo]:
        path = staging.stage(doc.id, data)
        try:
            return _page_infos(session, doc, path, settings)
        except BaseException:
            staging.discard(doc.id)  # e.g. password-protected: nothing stays behind
            raise

    pages = await run_in_threadpool(_stage)
    return schemas.TaxSourceResponse(pages=pages, expires_at=staging.expires_at(doc.id) or "")


@router.get(
    "/tax/documents/{document_id}/pages",
    response_model=list[schemas.TaxPageInfo],
    dependencies=_DOC_OWNER,
)
def list_document_pages(
    document_id: int, session: SessionDep, settings: SettingsDep
) -> list[schemas.TaxPageInfo]:
    """Per-page facts for choosing pages to read with the LLM: size, whether the automatic
    fallback would send it, and why a page can never be sent. Local-only; no network."""
    doc, pdf = _document_with_pdf(session, document_id)
    return _page_infos(session, doc, pdf, settings)


@router.get("/tax/documents/{document_id}/pages/{page_number}/image", dependencies=_DOC_OWNER)
def document_page_image(
    document_id: int, page_number: int, session: SessionDep, width: int = 400
) -> Response:
    """PNG thumbnail of one page of the kept PDF (local-only; same as the statements one)."""
    _doc, pdf = _document_with_pdf(session, document_id)
    width = max(100, min(1600, width))
    try:
        png = statement_preview.render_page_png(pdf, page_number, width)
    except statement_preview.PagePreviewNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return Response(
        content=png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"}
    )


@router.get(
    "/tax/documents/{document_id}/llm-preview",
    response_model=schemas.LlmParsePreview,
    dependencies=_DOC_OWNER,
)
def llm_preview(
    document_id: int, session: SessionDep, settings: SettingsDep, pages: str | None = None
) -> schemas.LlmParsePreview:
    """Exactly the redacted text the LLM would receive for ``pages`` (default: the pages the
    automatic fallback would send). No network call; works even when the automatic fallback is
    off. Without a configured LLM, ``mode`` is ``none`` and nothing is listed."""
    doc, pdf = _document_with_pdf(session, document_id)
    selection: list[int] | None = None
    if pages:
        try:
            selection = parse_page_spec(pages, doc.page_count)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        data = tax_llm.preview_pages(session, doc, pdf, selection, settings)
    except SpendAnalyzerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return schemas.LlmParsePreview(
        mode=data.mode,
        resolved_url=data.resolved_url,
        model=data.model,
        pages=data.pages,
        requests=data.requests,
        total_chars=data.total_chars,
        redactions=data.redactions,
    )


@router.post(
    "/tax/documents/{document_id}/llm-fallback",
    response_model=schemas.TaxLlmReadResponse,
    dependencies=_DOC_OWNER,
)
def read_pages_with_llm(
    document_id: int, body: schemas.TaxLlmReadRequest, session: SessionDep, settings: SettingsDep
) -> schemas.TaxLlmReadResponse:
    """Send exactly the chosen pages (redacted like the preview) to the configured LLM and merge
    what it reads into this document. Explicit per-document consent: allowed whenever the LLM
    is configured, whatever ``[ingest] tax_llm_fallback`` says. Form 1040 pages and pages without
    text are never sent. New forms are tagged as read by the LLM (needs review)."""
    doc, pdf = _document_with_pdf(session, document_id)
    extractor = tax_llm.build_on_demand_extractor(session, doc.user_id, settings)
    if extractor is None:
        raise HTTPException(status_code=409, detail="No LLM is configured (Settings, LLM).")
    terms = tax_llm.pii_terms_for(session, doc.user_id, settings)
    try:
        result = on_demand.read_pages(
            session, doc, pdf, extractor, pages=body.pages, pii_terms=terms
        )
    except on_demand.OnDemandError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except SpendAnalyzerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if _stored_pdf(doc) is None:
        staging.discard(doc.id)  # the re-selected copy was only for this read
    session.refresh(doc)
    return schemas.TaxLlmReadResponse(
        document=_document_detail(doc),
        pages=list(result.pages),
        requests_sent=result.requests_sent,
        inserted_forms=result.inserted_forms,
        skipped_duplicate_forms=result.skipped_duplicate_forms,
        merged_forms=result.merged_forms,
        income_item_count=result.income_item_count,
        tax_item_count=result.tax_item_count,
        warnings=list(result.warnings),
    )


@router.delete("/tax/documents/{document_id}", status_code=204, dependencies=_DOC_OWNER)
def delete_document(document_id: int, session: SessionDep) -> Response:
    if not delete_tax_document(session, document_id):
        raise HTTPException(status_code=404, detail="tax document not found")
    return Response(status_code=204)


@router.get("/tax/years", response_model=list[int])
def list_years(
    session: SessionDep, user_ids: Annotated[list[int] | None, Query()] = None
) -> list[int]:
    return tax_query.list_tax_years(session, user_ids)


def _income_amounts(summary: tax_query.TaxYearSummary) -> list[schemas.IncomeCategoryAmount]:
    return [
        schemas.IncomeCategoryAmount(
            category=schemas.IncomeCategoryEnum(cat),
            label=INCOME_CATEGORY_LABELS[cat],
            amount_minor=amount,
            is_memo=cat in MEMO_INCOME_CATEGORIES,
        )
        for cat, amount in summary.income_by_category.items()
    ]


def _tax_amounts(values: Mapping[TaxCategory, int]) -> list[schemas.TaxCategoryAmount]:
    return [
        schemas.TaxCategoryAmount(
            category=schemas.TaxCategoryEnum(cat),
            label=TAX_CATEGORY_LABELS[cat],
            amount_minor=amount,
        )
        for cat, amount in values.items()
    ]


def _summary(s: tax_query.TaxYearSummary) -> schemas.TaxYearSummary:
    return schemas.TaxYearSummary(
        tax_year=s.tax_year,
        user_id=s.user_id,
        income_basis=s.income_basis,
        income_by_category=_income_amounts(s),
        memo_by_category=[
            schemas.TaxMemoAmount(category=k, label=_MEMO_LABELS.get(k, k), amount_minor=v)
            for k, v in s.memo_by_category.items()
        ],
        total_income_minor=s.total_income_minor,
        withheld=_tax_amounts(s.withheld),
        fica=_tax_amounts(s.fica),
        liability=_tax_amounts(s.liability),
        payments_minor=s.payments_minor,
        refund_minor=s.refund_minor,
        amount_owed_minor=s.amount_owed_minor,
        refund_or_owed_derived=s.refund_or_owed_derived,
        federal_effective_rate=s.federal_effective_rate,
        all_in_rate=s.all_in_rate,
        withholding_rate=s.withholding_rate,
        document_count=s.document_count,
        form_count=s.form_count,
        warnings=list(s.warnings),
    )


@router.get("/tax/summary", response_model=list[schemas.TaxYearSummary])
def tax_summary(
    session: SessionDep,
    settings: SettingsDep,
    user_ids: Annotated[list[int] | None, Query()] = None,
    tax_years: Annotated[list[int] | None, Query()] = None,
    currency: str = "USD",
    per_user: bool = False,
) -> list[schemas.TaxYearSummary]:
    """One summary per tax year (newest first); with `per_user`, one per (year, user)."""
    tax_params_update.maybe_check_in_background(settings)  # opt-in, once a year, never blocks
    query = TaxQuery(user_ids=user_ids, tax_years=tax_years, currency=currency, per_user=per_user)
    try:
        rows = tax_query.run_tax_summary(session, query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return [_summary(r) for r in rows]


@router.get("/tax/income-by-payer", response_model=list[schemas.TaxIncomePayer])
def income_by_payer(
    session: SessionDep,
    user_ids: Annotated[list[int] | None, Query()] = None,
    tax_years: Annotated[list[int] | None, Query()] = None,
    currency: str = "USD",
) -> list[schemas.TaxIncomePayer]:
    """Non-memo income on source forms (W-2 / 1099) per payer; newest year, largest first."""
    query = TaxQuery(user_ids=user_ids, tax_years=tax_years, currency=currency)
    try:
        rows = tax_query.run_income_by_payer(session, query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return [
        schemas.TaxIncomePayer(
            tax_year=r.tax_year,
            user_id=r.user_id,
            payer_name=r.payer_name,
            form_type=schemas.TaxFormType(r.form_type),
            category=schemas.IncomeCategoryEnum(r.category),
            label=INCOME_CATEGORY_LABELS[r.category],
            amount_minor=r.amount_minor,
        )
        for r in rows
    ]


# --- manual edits ---------------------------------------------------------------------------------
#
# Figures can be typed in without a PDF, or corrected after an import. All writes go through
# `ingest/tax/edit.py` (validation, provenance); errors map to HTTP status via `EditError.status`.
# Summaries read the tables live, so an edit shows up in the next `/tax/summary` call.


def _raise(exc: edit.EditError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


def _patch_values(model: BaseModel) -> dict[str, object]:
    """The fields the client actually sent, as plain values (enums unwrapped)."""
    out: dict[str, object] = {}
    for name in model.model_fields_set:
        value = getattr(model, name)
        out[name] = value.value if isinstance(value, StrEnum) else value
    return out


def _form_detail(form: TaxForm) -> schemas.TaxFormDetail:
    return schemas.TaxFormDetail(
        **_form_fields(form),
        income_items=[_income_item(i) for i in form.income_items],
        tax_items=[_tax_item(t) for t in form.tax_items],
    )


def _income_data(body: schemas.IncomeItemIn) -> edit.IncomeItemData:
    return edit.IncomeItemData(
        category=body.category.value,
        amount_minor=body.amount_minor,
        box=body.box,
        label=body.label,
        is_memo=body.is_memo,
        source_form=body.source_form,
    )


def _tax_data(body: schemas.TaxItemIn) -> edit.TaxItemData:
    return edit.TaxItemData(
        category=body.category.value,
        amount_minor=body.amount_minor,
        box=body.box,
        label=body.label,
        jurisdiction=body.jurisdiction,
        is_estimate=body.is_estimate,
        source_form=body.source_form,
    )


@router.post("/tax/documents/manual", response_model=schemas.TaxDocument, status_code=201)
def create_manual_document(
    body: schemas.TaxManualDocumentCreate, session: SessionDep
) -> schemas.TaxDocument:
    """A document with no file, to hold figures typed in by the user."""
    try:
        doc = edit.create_manual_document(session, body.user_id, body.tax_year, body.original_name)
    except edit.EditError as exc:
        raise _raise(exc) from None
    return _document(doc)


@router.patch(
    "/tax/documents/{document_id}",
    response_model=schemas.TaxDocument,
    dependencies=_DOC_OWNER,
)
def reassign_document(
    document_id: int, body: schemas.TaxDocumentPatch, session: SessionDep
) -> schemas.TaxDocument:
    """Move a document with all its forms and items to another user (`user_id` in the query is
    the ownership check on the current owner; `user_id` in the body is the new owner)."""
    try:
        doc = edit.reassign_document(session, document_id, body.user_id)
    except edit.EditError as exc:
        raise _raise(exc) from None
    return _document(doc)


@router.post(
    "/tax/documents/{document_id}/forms",
    response_model=schemas.TaxFormDetail,
    status_code=201,
    dependencies=_DOC_OWNER,
)
def create_form(
    document_id: int, body: schemas.TaxFormCreate, session: SessionDep
) -> schemas.TaxFormDetail:
    """Add a form (with optional items) to a document. A document that was `error` /
    `unsupported` becomes `parsed`."""
    data = edit.FormData(
        form_type=body.form_type.value,
        tax_year=body.tax_year,
        payer_name=body.payer_name,
        payer_tin_last4=body.payer_tin_last4,
        account_last4=body.account_last4,
        is_corrected=body.is_corrected,
        filing_status=body.filing_status,
        agi_minor=body.agi_minor,
        taxable_income_minor=body.taxable_income_minor,
        income_items=tuple(_income_data(i) for i in body.income_items),
        tax_items=tuple(_tax_data(t) for t in body.tax_items),
    )
    try:
        form = edit.add_form(session, document_id, data)
    except edit.EditError as exc:
        raise _raise(exc) from None
    return _form_detail(form)


@router.patch(
    "/tax/forms/{form_id}", response_model=schemas.TaxFormDetail, dependencies=_FORM_OWNER
)
def patch_form(
    form_id: int, body: schemas.TaxFormPatch, session: SessionDep
) -> schemas.TaxFormDetail:
    try:
        form = edit.update_form(session, form_id, _patch_values(body))
    except edit.EditError as exc:
        raise _raise(exc) from None
    return _form_detail(form)


@router.delete("/tax/forms/{form_id}", status_code=204, dependencies=_FORM_OWNER)
def delete_form(form_id: int, session: SessionDep) -> Response:
    try:
        edit.delete_form(session, form_id)
    except edit.EditError as exc:
        raise _raise(exc) from None
    return Response(status_code=204)


@router.post(
    "/tax/forms/{form_id}/confirm",
    response_model=schemas.TaxFormDetail,
    dependencies=_FORM_OWNER,
)
def confirm_form(form_id: int, session: SessionDep) -> schemas.TaxFormDetail:
    """Mark a reviewed form's AI-extracted items as confirmed (`origin` becomes `manual`)."""
    try:
        form = edit.confirm_form(session, form_id)
    except edit.EditError as exc:
        raise _raise(exc) from None
    return _form_detail(form)


@router.post(
    "/tax/forms/{form_id}/income-items",
    response_model=schemas.IncomeItemOut,
    status_code=201,
    dependencies=_FORM_OWNER,
)
def create_income_item(
    form_id: int, body: schemas.IncomeItemIn, session: SessionDep
) -> schemas.IncomeItemOut:
    try:
        item = edit.add_income_item(session, form_id, _income_data(body))
    except edit.EditError as exc:
        raise _raise(exc) from None
    return _income_item(item)


@router.patch(
    "/tax/income-items/{item_id}",
    response_model=schemas.IncomeItemOut,
    dependencies=_INCOME_OWNER,
)
def patch_income_item(
    item_id: int, body: schemas.IncomeItemPatch, session: SessionDep
) -> schemas.IncomeItemOut:
    try:
        item = edit.update_income_item(session, item_id, _patch_values(body))
    except edit.EditError as exc:
        raise _raise(exc) from None
    return _income_item(item)


@router.delete("/tax/income-items/{item_id}", status_code=204, dependencies=_INCOME_OWNER)
def delete_income_item(item_id: int, session: SessionDep) -> Response:
    try:
        edit.delete_income_item(session, item_id)
    except edit.EditError as exc:
        raise _raise(exc) from None
    return Response(status_code=204)


@router.post(
    "/tax/forms/{form_id}/tax-items",
    response_model=schemas.TaxItemOut,
    status_code=201,
    dependencies=_FORM_OWNER,
)
def create_tax_item(
    form_id: int, body: schemas.TaxItemIn, session: SessionDep
) -> schemas.TaxItemOut:
    try:
        item = edit.add_tax_item(session, form_id, _tax_data(body))
    except edit.EditError as exc:
        raise _raise(exc) from None
    return _tax_item(item)


@router.patch(
    "/tax/tax-items/{item_id}", response_model=schemas.TaxItemOut, dependencies=_TAX_ITEM_OWNER
)
def patch_tax_item(
    item_id: int, body: schemas.TaxItemPatch, session: SessionDep
) -> schemas.TaxItemOut:
    try:
        item = edit.update_tax_item(session, item_id, _patch_values(body))
    except edit.EditError as exc:
        raise _raise(exc) from None
    return _tax_item(item)


@router.delete("/tax/tax-items/{item_id}", status_code=204, dependencies=_TAX_ITEM_OWNER)
def delete_tax_item(item_id: int, session: SessionDep) -> Response:
    try:
        edit.delete_tax_item(session, item_id)
    except edit.EditError as exc:
        raise _raise(exc) from None
    return Response(status_code=204)
