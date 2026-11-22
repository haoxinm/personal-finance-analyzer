"""`/api/statements` (§3.12, A22 two-phase import).

Upload hardening (A12): a 25 MB cap enforced while streaming (never buffers an oversized file
whole), a `%PDF-` magic-byte check, a `sha256`-derived stored filename (never the user-supplied
name), and a sanitized `original_name` before it is ever echoed back.

Calls into `api/services/gateway.py` for the §3.12a `ingest/pipeline.py` interface (P2-A); never
imports `ingest.pipeline` directly (see that module's docstring).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy.orm import Session

from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import (
    JobRunnerDep,
    SessionDep,
    SessionFactoryDep,
    SettingsDep,
)
from personal_finance_analyzer.api.services import crud, gateway, job_tasks, statement_preview
from personal_finance_analyzer.api.services.job_tasks import enqueue_import_then_classify
from personal_finance_analyzer.api.services.uploads import (
    read_pdf_upload,
    sanitize_original_name,
    save_upload,
)
from personal_finance_analyzer.core.errors import PfaError
from personal_finance_analyzer.db.models import Account, LayoutSpec, Statement, User
from personal_finance_analyzer.ingest.extract import extract
from personal_finance_analyzer.ingest.pages import load_pages, parse_page_spec, validate_pages

router = APIRouter(tags=["statements"])


def _attention(
    statement: Statement, *, reconciliation_delta: int | None, pdf_missing: bool
) -> list[schemas.StatementAttention]:
    """What a *parsed* statement needs a second look for (empty for any other status)."""
    if statement.status != "parsed":
        return []
    notes: list[schemas.StatementAttention] = []
    if statement.parsed_row_count == 0 or (
        statement.parsed_row_count is None and not statement.txn_count
    ):
        # (A statement imported before row counts were recorded and with no rows: whether it
        # found nothing or only duplicates is unknown, but either way it has no transactions.)
        notes.append("no_transactions")
    elif statement.parsed_row_count is not None and not statement.txn_count:
        notes.append("all_duplicates")
    if reconciliation_delta:
        notes.append("reconciliation_mismatch")
    if statement.shape_warnings:
        notes.append("layout_drift")
    if pdf_missing:
        notes.append("pdf_missing")
    return notes


def _statement_response(session: Session, statement: Statement) -> schemas.Statement:
    account = session.get(Account, statement.account_id) if statement.account_id else None
    delta = crud.reconciliation_delta_minor(session, statement)
    pdf_missing = crud.statement_pdf_missing(statement, search=False)
    return schemas.Statement(
        id=statement.id,
        user_id=crud.statement_user_id(session, statement),
        account_id=statement.account_id,
        issuer_id=statement.issuer_id,
        parser_id=statement.parser_id,
        layout_spec_id=statement.layout_spec_id,
        page_count=statement.page_count,
        selected_pages=load_pages(statement.selected_pages),
        status=statement.status,
        period_start=statement.period_start,
        period_end=statement.period_end,
        txn_count=statement.txn_count,
        reconciliation_delta_minor=delta,
        detect_score=statement.detect_score,
        shape_warnings=statement.shape_warnings,
        error_detail=statement.error_detail,
        created_at=statement.ingested_at,
        original_name=statement.original_name,
        has_pdf=statement.stored_path is not None,
        pdf_in_place=statement.pdf_in_place,
        parsed_row_count=statement.parsed_row_count,
        attention=_attention(statement, reconciliation_delta=delta, pdf_missing=pdf_missing),
        currency=account.currency if account is not None else None,
        pdf_missing=pdf_missing,
    )


def _proposal_schema(proposal: gateway.ImportProposal) -> schemas.ExtractorProposal:
    return schemas.ExtractorProposal(
        issuer_id=proposal.issuer_id,
        parser_id=proposal.parser_id,
        layout_spec_id=proposal.layout_spec_id,
        confidence=proposal.detect_score or 0.0,
        auto_confirmed=False,
        remembered=proposal.remembered,
    )


@router.post("/statements", response_model=schemas.StatementUploadResponse, status_code=201)
async def upload_statement(
    file: UploadFile,
    session: SessionDep,
    user_id: int = Form(...),
) -> schemas.StatementUploadResponse:
    """Multipart upload. `user_id` (D4: chosen in the Import screen, sticky) is a form field
    alongside `file`."""
    data = await read_pdf_upload(file)
    original_name = sanitize_original_name(file.filename or "statement.pdf")
    pdf_path = save_upload(data)

    proposal = gateway.propose_import(
        session, pdf_path, user_id=user_id, original_name=original_name
    )
    session.commit()

    statement = crud.get_statement(session, proposal.statement_id)
    if statement is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail="propose_import did not create a statement")

    return schemas.StatementUploadResponse(
        statement=_statement_response(session, statement),
        proposal=_proposal_schema(proposal),
        duplicate=proposal.status == "duplicate",
    )


@router.get("/statements", response_model=list[schemas.Statement])
def list_statements(session: SessionDep, user_id: int | None = None) -> list[schemas.Statement]:
    return [_statement_response(session, s) for s in crud.list_statements(session, user_id=user_id)]


@router.get("/statements/{statement_id}", response_model=schemas.Statement)
def get_statement(statement_id: int, session: SessionDep) -> schemas.Statement:
    statement = crud.get_statement(session, statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="statement not found")
    return _statement_response(session, statement)


@router.patch("/statements/{statement_id}", response_model=schemas.StatementPatchResult)
def patch_statement(
    statement_id: int, body: schemas.StatementPatch, session: SessionDep
) -> schemas.StatementPatchResult:
    """Move a statement to another person and/or account ("change person").

    * `user_id` alone: the statement and all its transactions move to that person's matching
      account (same issuer, type, mask and currency), which is created when they have none.
    * `account_id` alone: they move to that account; its owner becomes the statement's person.
    * both: moved to that account; the account must belong to the given user (400 otherwise).
      An `account_id` needs an imported statement (409 otherwise) and an existing account (404).

    Duplicates the target account already holds are merged (`duplicates_deleted`). Sending
    nothing, or what is already the case, is a 400.
    """
    statement = crud.get_statement(session, statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="statement not found")
    if body.user_id is None and body.account_id is None:
        raise HTTPException(status_code=400, detail="give a user_id or an account_id to move to")

    owner = crud.statement_user_id(session, statement)
    rows_moved = duplicates_deleted = 0
    if body.account_id is not None:
        account = session.get(Account, body.account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        if statement.status != "parsed":
            raise HTTPException(
                status_code=409,
                detail="this statement is not imported yet; finish importing it first",
            )
        if body.user_id is not None and body.user_id != account.user_id:
            raise HTTPException(status_code=400, detail="that account belongs to someone else")
        user_id = account.user_id
        if body.account_id == statement.account_id and user_id == owner:
            raise HTTPException(status_code=400, detail="nothing to change")
        # `reassign` (§3.5) recomputes `dedupe_hash` for every one of this statement's rows and
        # updates `statement.account_id` itself; a plain CRUD field write would race it and could
        # set an account_id its own dedupe recompute never ran against.
        rows_moved, duplicates_deleted = gateway.reassign(
            session, statement_id, user_id=user_id, account_id=body.account_id
        )
    else:
        assert body.user_id is not None
        if session.get(User, body.user_id) is None:
            raise HTTPException(status_code=404, detail="user not found")
        if body.user_id == owner:
            raise HTTPException(status_code=400, detail="the statement already belongs to them")
        if statement.account_id is None:
            statement.user_id = body.user_id  # not imported yet: it is imported for them
        else:
            target = gateway.resolve_account_for_move(session, statement_id, user_id=body.user_id)
            rows_moved, duplicates_deleted = gateway.reassign(
                session, statement_id, user_id=body.user_id, account_id=target.id
            )
    session.commit()
    session.refresh(statement)
    return schemas.StatementPatchResult(
        **_statement_response(session, statement).model_dump(),
        rows_moved=rows_moved,
        duplicates_deleted=duplicates_deleted,
    )


@router.delete("/statements/{statement_id}", status_code=204)
def delete_statement(
    statement_id: int,
    session: SessionDep,
    delete_pdf: Annotated[
        bool,
        Query(
            description="Also delete the PDF file when it is one you put in the documents "
            "folder yourself (kept by default). The app's own copy is always deleted."
        ),
    ] = False,
) -> None:
    statement = crud.get_statement(session, statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="statement not found")
    in_place = statement.pdf_in_place
    stored_path, sha256 = crud.delete_statement(session, statement)
    session.commit()
    crud.discard_pdf_copy(session, stored_path, sha256, in_place=in_place, delete_file=delete_pdf)


@router.get("/statements/{statement_id}/preview", response_model=schemas.StatementPreviewPage)
def preview_statement_page(
    statement_id: int, session: SessionDep, page: int = 1
) -> schemas.StatementPreviewPage:
    """One page's words (`x0`/`x1`/`top`/`bottom`) plus page size, for the Layout mapper's
    click-to-map UI (§2c). Local-only: this stays on the token-protected localhost API and never
    goes through egress (I1b/I3)."""
    statement = crud.get_statement(session, statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="statement not found")

    pdf_path = crud.staged_pdf_path(statement)
    if pdf_path is None:
        raise _pdf_unavailable(statement)

    try:
        page_preview = statement_preview.preview_page(pdf_path, page)
    except statement_preview.PagePreviewNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None

    return schemas.StatementPreviewPage(
        page_number=page_preview.page_number,
        width=page_preview.width,
        height=page_preview.height,
        words=[
            schemas.StatementPreviewWord(text=w.text, x0=w.x0, x1=w.x1, top=w.top, bottom=w.bottom)
            for w in page_preview.words
        ],
    )


def _pdf_unavailable(statement: Statement) -> HTTPException:
    """404 for a statement whose PDF cannot be read. ``code: pdf_not_found`` when a copy was kept
    but is gone from the documents folder (the UI says so), ``pdf_not_kept`` when none was kept."""
    if crud.statement_pdf_missing(statement):
        return HTTPException(
            status_code=404,
            detail={
                "code": "pdf_not_found",
                "message": (
                    "PDF not found: the copy of this statement is no longer in your documents "
                    "folder (it was moved out or deleted). Put it back, or import it again."
                ),
            },
        )
    return HTTPException(
        status_code=404,
        detail={
            "code": "pdf_not_kept",
            "message": (
                "no PDF copy is available for this statement "
                "([privacy] store_pdf_copies was off and it is already confirmed)"
            ),
        },
    )


def _pdf_or_404(statement_id: int, session: Session) -> tuple[Statement, Path]:
    statement = crud.get_statement(session, statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="statement not found")
    pdf_path = crud.staged_pdf_path(statement)
    if pdf_path is None:
        raise _pdf_unavailable(statement)
    return statement, pdf_path


@router.get("/statements/{statement_id}/pages", response_model=list[schemas.StatementPageInfo])
def list_statement_pages(statement_id: int, session: SessionDep) -> list[schemas.StatementPageInfo]:
    """Per-page text stats and a snippet, so the user can choose which pages to keep/parse."""
    statement, pdf_path = _pdf_or_404(statement_id, session)
    rows = statement_preview.page_infos(pdf_path, load_pages(statement.selected_pages))
    return [
        schemas.StatementPageInfo(
            page_number=n, char_count=chars, snippet=snippet, selected=is_selected
        )
        for n, chars, snippet, is_selected in rows
    ]


@router.get("/statements/{statement_id}/pages/{page_number}/image")
def statement_page_image(
    statement_id: int, page_number: int, session: SessionDep, width: int = 400
) -> Response:
    """PNG thumbnail of one page (loaded via `<img src=...?token=...>`; local-only)."""
    _statement, pdf_path = _pdf_or_404(statement_id, session)
    width = max(100, min(1600, width))
    try:
        png = statement_preview.render_page_png(pdf_path, page_number, width)
    except statement_preview.PagePreviewNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return Response(
        content=png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"}
    )


@router.get("/statements/{statement_id}/llm-preview", response_model=schemas.LlmParsePreview)
def llm_preview(
    statement_id: int, session: SessionDep, settings: SettingsDep, pages: str | None = None
) -> schemas.LlmParsePreview:
    """Exactly the (redacted) page text the LLM statement parser would send. No network call."""
    from personal_finance_analyzer.llm import statement_parser

    statement, pdf_path = _pdf_or_404(statement_id, session)
    selection: list[int] | None
    if pages:
        try:
            selection = parse_page_spec(pages, statement.page_count)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    else:
        selection = load_pages(statement.selected_pages)
    try:
        doc = extract(pdf_path).select_pages(selection)
    except PfaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    terms = statement_preview.llm_pii_terms(session, settings, statement.user_id)
    requests = statement_parser.preview(doc, settings.llm, terms)
    redactions = statement_parser.count_redactions(doc, terms)
    return schemas.LlmParsePreview(
        mode=settings.llm.mode,
        resolved_url=settings.llm.resolved_url if settings.llm.is_configured else "",
        model=settings.llm.model,
        pages=[p.page_number for p in doc.pages],
        requests=requests,
        total_chars=sum(len(r) for r in requests),
        redactions=redactions,
    )


@router.get(
    "/statements/{statement_id}/extractors", response_model=list[schemas.ExtractorCandidate]
)
def list_statement_extractors(
    statement_id: int, session: SessionDep
) -> list[schemas.ExtractorCandidate]:
    """Every extractor the statement could be (re-)imported with, scored on its saved pages:
    the built-in parsers, the approved layout specs of its issuer, and the LLM when configured.
    The recorded one first, then best match first. Local only; no LLM call."""
    _statement, _pdf = _pdf_or_404(statement_id, session)
    return [
        schemas.ExtractorCandidate.model_validate(dataclasses.asdict(c))
        for c in gateway.extractor_candidates(session, statement_id)
    ]


@router.post("/statements/{statement_id}/propose", response_model=schemas.StatementUploadResponse)
def repropose_statement(statement_id: int, session: SessionDep) -> schemas.StatementUploadResponse:
    """Re-run issuer matching and extractor scoring for a statement that is not parsed yet, so a
    choice made since it was uploaded (a remembered extractor, a new issuer or layout) is picked
    up. Updates the statement's issuer, extractor and score; never calls the LLM."""
    statement = crud.get_statement(session, statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="statement not found")
    if statement.status == "parsed":
        raise HTTPException(status_code=409, detail="this statement is already imported")
    _pdf_or_404(statement_id, session)
    try:
        proposal = gateway.repropose(session, statement_id)
    except PfaError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    session.commit()
    session.refresh(statement)
    return schemas.StatementUploadResponse(
        statement=_statement_response(session, statement),
        proposal=_proposal_schema(proposal),
        duplicate=False,
    )


@router.post(
    "/statements/{statement_id}/extract", response_model=schemas.JobIdResponse, status_code=202
)
def extract_statement(
    statement_id: int,
    body: schemas.ExtractRequest,
    session: SessionDep,
    settings: SettingsDep,
    runner: JobRunnerDep,
    session_factory: SessionFactoryDep,
) -> schemas.JobIdResponse:
    """A22 phase 2: enqueues an `import` job that calls `confirm_import`, then (job flow, P2-C
    plan section) enqueues a `classify` job for the transactions it inserted."""
    statement = crud.get_statement(session, statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="statement not found")
    if body.remember and body.layout_spec_id is not None:
        problem = crud.remembered_spec_problem(session, body.issuer_id, body.layout_spec_id)
        if problem is not None:
            raise HTTPException(status_code=400, detail=problem)

    job_id = enqueue_import_then_classify(
        runner,
        session_factory,
        statement_id=statement_id,
        issuer_id=body.issuer_id,
        parser_id=body.parser_id,
        layout_spec_id=body.layout_spec_id,
        remember=body.remember,
        cfg=settings.llm,
        pages=body.pages,
    )
    return schemas.JobIdResponse(job_id=job_id)


@router.post(
    "/statements/{statement_id}/reparse", response_model=schemas.JobIdResponse, status_code=202
)
def reparse_statement(
    statement_id: int,
    body: schemas.ReparseRequest,
    session: SessionDep,
    runner: JobRunnerDep,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
) -> schemas.JobIdResponse:
    """Rebuild a statement's transactions from its kept PDF ("re-import"), then classify them.

    With no extractor in the body the statement's recorded one is used (its layout spec, else
    its parser). The user's edits are restored onto the rebuilt rows unless `discard_edits`.
    Problems that can be known up front are synchronous 4xx errors rather than failed jobs.
    """
    statement = crud.get_statement(session, statement_id)
    if statement is None:
        raise HTTPException(status_code=404, detail="statement not found")
    if body.parser_id is not None and body.layout_spec_id is not None:
        raise HTTPException(
            status_code=400, detail="give either parser_id or layout_spec_id, not both"
        )
    if statement.account_id is None:
        raise HTTPException(
            status_code=409,
            detail="this statement was never imported; finish it on the Import page",
        )
    parser_id, layout_spec_id = body.parser_id, body.layout_spec_id
    if parser_id is None and layout_spec_id is None:
        layout_spec_id = statement.layout_spec_id
        if layout_spec_id is None:
            parser_id = statement.parser_id
        if layout_spec_id is None and parser_id is None:
            raise HTTPException(
                status_code=400,
                detail="this statement has no extractor on record; choose one",
            )
        if not gateway.extractor_exists(
            session, parser_id=parser_id, layout_spec_id=layout_spec_id
        ):
            raise HTTPException(
                status_code=400,
                detail="this statement's recorded extractor is no longer available; choose one",
            )
    else:
        # An explicit choice must be usable: fail now, not later as a job error.
        if layout_spec_id is not None:
            spec = session.get(LayoutSpec, layout_spec_id)
            if spec is None or not spec.approved:
                raise HTTPException(
                    status_code=400, detail="that layout spec does not exist or is not approved"
                )
        elif not gateway.extractor_exists(session, parser_id=parser_id, layout_spec_id=None):
            raise HTTPException(status_code=400, detail=f"unknown extractor {parser_id!r}")
    if body.pages is not None:
        try:
            if statement.page_count is not None:
                validate_pages(body.pages, statement.page_count)
            elif not body.pages:
                raise PfaError("no pages selected")
        except PfaError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    if statement.stored_path is None:
        raise HTTPException(
            status_code=409,
            detail="no copy of this statement's PDF was kept; delete it and import the PDF again",
        )
    if crud.kept_pdf_path(statement) is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pdf_not_found",
                "message": (
                    "PDF not found: the copy of this statement is no longer in your documents "
                    "folder (it was moved out or deleted). Put it back, or delete the statement "
                    "and drop the PDF again."
                ),
            },
        )

    job_id = job_tasks.enqueue_reparse_then_classify(
        runner,
        session_factory,
        statement_id=statement_id,
        parser_id=parser_id,
        layout_spec_id=layout_spec_id,
        pages=body.pages,
        discard_edits=body.discard_edits,
        cfg=settings.llm,
        allow_empty=body.allow_empty,
    )
    return schemas.JobIdResponse(job_id=job_id)
