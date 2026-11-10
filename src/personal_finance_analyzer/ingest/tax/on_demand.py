"""Page-level planning and on-demand LLM reading for an already imported tax document.

Two uses, both starting from the PDF copy kept under ``[privacy] store_tax_pdf_copies``:

- `load_plan` answers "which pages would the fallback send (by default), and which pages may a
  user pick?" without any network call (the preview and the page list use it);
- `read_pages` runs an injected `TaxFallbackExtractor` over exactly the pages the user chose
  (explicit per-document consent) and merges the forms it returns into the existing document the
  way `fallback.apply_fallback` does at import time: deterministic results win, a re-read of a
  form that is already stored is dropped, items of a consolidated statement go into the matching
  stored form, everything new is tagged ``llm`` (so it shows "needs review").

The extractor is injected (`core.tax_types.TaxFallbackExtractor`); this module imports no LLM
code. The network call happens before any database write, never inside a write transaction.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from personal_finance_analyzer.core.errors import (
    EgressViolation,
    ParserError,
    PfaError,
    UnsupportedLayoutError,
)
from personal_finance_analyzer.core.logging import get_logger
from personal_finance_analyzer.core.tax_types import (
    ParsedTaxForm,
    TaxFallbackExtractor,
    UnparsedRegion,
)
from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.db.models import IncomeItem, TaxDocument, TaxItem
from personal_finance_analyzer.ingest.extract import extract
from personal_finance_analyzer.ingest.tax.common import (
    MAX_TAX_YEAR,
    MIN_TAX_YEAR,
    check_warning,
    parsing_with_pii_terms,
)
from personal_finance_analyzer.ingest.tax.fallback import (
    REVIEW_WARNING,
    _merge_forms,
    fallback_targets,
    other_form_pages,
)
from personal_finance_analyzer.ingest.tax.normalize import normalize_doc
from personal_finance_analyzer.ingest.tax.orchestrator import ParseOutcome, parse_document
from personal_finance_analyzer.ingest.tax.pipeline import (
    STATUS_PARSED,
    store_forms,
)
from personal_finance_analyzer.ingest.tax.year import detect_tax_year

logger = get_logger("ingest.tax.on_demand")

#: Ordinal base for weak nameless forms stored by an on-demand read (a repeat of the same read
#: produces the same fingerprints and is skipped as a duplicate).
_ON_DEMAND_ORDINAL = 10_000

NO_PAGES_MESSAGE = "None of the selected pages can be sent to the LLM."
NO_YEAR_MESSAGE = "The tax year of this document is not known, so the LLM result cannot be stored."
LLM_FAILED_MESSAGE = "The LLM request failed."


class OnDemandError(PfaError):
    """A request that cannot be served; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class PageFacts:
    page_number: int
    char_count: int
    #: Would the automatic fallback send (part of) this page?
    suggested: bool
    #: Why this page can never be sent (`None` = it can).
    excluded_reason: str | None


@dataclass(frozen=True, slots=True)
class PagePlan:
    extracted: ExtractedDoc
    #: The regions to send for the chosen (or default) pages, cropped as the fallback would.
    regions: tuple[UnparsedRegion, ...]
    #: The pages of `regions` that exist and may be sent, ascending.
    pages: tuple[int, ...]
    #: Pages the automatic fallback would send for this file.
    default_pages: tuple[int, ...]
    #: The deterministic result (`None` when nothing parsed), for dedupe.
    outcome: ParseOutcome | None
    tax_year: int | None
    facts: tuple[PageFacts, ...]


def _excluded_reason(extracted: ExtractedDoc) -> dict[int, str]:
    out: dict[int, str] = {}
    other = other_form_pages(extracted)
    for page in extracted.pages:
        if not page.text.strip():
            out[page.page_number] = "No text on this page."
        elif page.page_number in other:
            out[page.page_number] = "Form 1040 pages are never sent."
    return out


def _parse(extracted: ExtractedDoc, pii_terms: Sequence[str]) -> ParseOutcome | None:
    if not extracted.has_text_layer:
        return None
    try:
        with parsing_with_pii_terms(pii_terms):
            return parse_document(extracted, tax_year=None)
    except (ParserError, UnsupportedLayoutError):
        return None
    except Exception as exc:  # a parser bug must not break the preview
        logger.warning("tax re-parse failed: %s", type(exc).__name__)
        return None


def _regions_for(
    default: Sequence[UnparsedRegion], chosen: Sequence[int]
) -> tuple[UnparsedRegion, ...]:
    """Regions for exactly `chosen`: pages the default fallback covers keep their regions (so they
    are cropped the same way); any other chosen page is sent whole."""
    wanted = set(chosen)
    regions: list[UnparsedRegion] = []
    covered: set[int] = set()
    for region in default:
        mine = tuple(p for p in region.pages if p in wanted)
        if not mine:
            continue
        covered.update(mine)
        regions.append(
            UnparsedRegion(
                kind=region.kind,
                pages=mine,
                reason=region.reason,
                boxes=tuple(b for b in region.boxes if b[0] in mine),
            )
        )
    rest = tuple(sorted(wanted - covered))
    if rest:
        regions.append(UnparsedRegion(kind="unknown", pages=rest, reason="user-selected"))
    return tuple(regions)


def load_plan(
    pdf_path: Path,
    *,
    pii_terms: Sequence[str],
    document_year: int | None = None,
    pages: Sequence[int] | None = None,
) -> PagePlan:
    """Plan the LLM read of the stored PDF at `pdf_path`. ``pages=None`` = what the fallback would
    send by default; explicit pages are sent as given (cropped like the default where they
    overlap it), minus pages without text and Form 1040 pages. No network."""
    extracted = extract(pdf_path, write_cache=False)
    outcome = _parse(extracted, pii_terms)
    default_regions, year = fallback_targets(extracted, outcome, tax_year=document_year)
    if year is None:
        year = document_year
    present = {p.page_number for p in extracted.pages}
    excluded = _excluded_reason(extracted)
    default_pages = tuple(
        sorted(
            {p for r in default_regions for p in r.pages if p in present and p not in excluded}
            if extracted.has_text_layer
            else set()
        )
    )
    if pages is None:
        regions = _regions_for(default_regions, default_pages) if extracted.has_text_layer else ()
        chosen = default_pages
    else:
        chosen = tuple(sorted({p for p in pages if p in present and p not in excluded}))
        regions = _regions_for(default_regions, chosen)
    facts = tuple(
        PageFacts(
            page_number=p.page_number,
            char_count=p.char_count,
            suggested=p.page_number in default_pages and p.page_number not in excluded,
            excluded_reason=excluded.get(p.page_number),
        )
        for p in extracted.pages
    )
    return PagePlan(
        extracted=extracted,
        regions=regions,
        pages=chosen,
        default_pages=default_pages,
        outcome=outcome,
        tax_year=year,
        facts=facts,
    )


@dataclass(frozen=True, slots=True)
class OnDemandResult:
    requests_sent: int
    inserted_forms: int
    skipped_duplicate_forms: int
    #: Existing consolidated forms that received new sections.
    merged_forms: int
    income_item_count: int
    tax_item_count: int
    warnings: tuple[str, ...]
    pages: tuple[int, ...]


def _append_sections(
    session: Session, document: TaxDocument, source: ParsedTaxForm, merged: ParsedTaxForm
) -> tuple[int, int]:
    """Add the sections `merged` gained over `source` to the stored consolidated form of
    `document` that matches `source`. Returns ``(income_added, tax_added)`` (0, 0 if no match)."""
    candidates = [f for f in document.forms if f.form_type == source.form_type]
    target = next(
        (
            f
            for f in candidates
            if (f.account_last4 is not None and f.account_last4 == source.account_last4)
            or (f.payer_tin_last4 is not None and f.payer_tin_last4 == source.payer_tin_last4)
        ),
        candidates[0] if len(candidates) == 1 else None,
    )
    if target is None:
        return 0, 0
    have_income = {(i.source_form, i.category, i.amount_minor) for i in target.income_items}
    have_tax = {(t.source_form, t.category, t.amount_minor) for t in target.tax_items}
    income_added = tax_added = 0
    for i in merged.income[len(source.income) :]:
        if (i.source_form, i.category, i.amount_minor) in have_income:
            continue
        session.add(
            IncomeItem(
                form_id=target.id,
                user_id=target.user_id,
                tax_year=target.tax_year,
                category=i.category,
                amount_minor=i.amount_minor,
                currency="USD",
                source_form=i.source_form,
                box=i.box,
                label=i.label,
                is_memo=i.is_memo,
                origin=i.origin,
            )
        )
        income_added += 1
    for t in merged.taxes[len(source.taxes) :]:
        if (t.source_form, t.category, t.amount_minor) in have_tax:
            continue
        session.add(
            TaxItem(
                form_id=target.id,
                user_id=target.user_id,
                tax_year=target.tax_year,
                category=t.category,
                amount_minor=t.amount_minor,
                currency="USD",
                source_form=t.source_form,
                box=t.box,
                label=t.label,
                jurisdiction=t.jurisdiction,
                is_estimate=t.is_estimate,
                origin=t.origin,
            )
        )
        tax_added += 1
    return income_added, tax_added


def read_pages(
    session: Session,
    document: TaxDocument,
    pdf_path: Path,
    fallback: TaxFallbackExtractor,
    *,
    pages: Sequence[int],
    pii_terms: Sequence[str],
) -> OnDemandResult:
    """Send exactly `pages` of the stored PDF through `fallback` and merge the forms it reads into
    `document` (commits). Raises `OnDemandError` when nothing can be sent or stored."""
    plan = load_plan(pdf_path, pii_terms=pii_terms, document_year=document.tax_year, pages=pages)
    if not plan.pages:
        raise OnDemandError(NO_PAGES_MESSAGE)
    year = document.tax_year
    if year is None:
        year = plan.tax_year
    if year is None:
        year = detect_tax_year(normalize_doc(plan.extracted))
    if year is None or not MIN_TAX_YEAR <= year <= MAX_TAX_YEAR:
        raise OnDemandError(NO_YEAR_MESSAGE)

    # The network call: only reads have happened on `session` so far, nothing is written yet.
    try:
        result = fallback.extract(plan.extracted, plan.regions, tax_year=year)
    except EgressViolation:
        raise
    except Exception as exc:
        logger.warning("tax on-demand llm read failed: %s", type(exc).__name__)
        raise OnDemandError(LLM_FAILED_MESSAGE, 502) from None

    det_forms = plan.outcome.document.forms if plan.outcome and plan.outcome.document else ()
    merged = _merge_forms(det_forms, result.forms)
    new_forms = merged[len(det_forms) :]

    session.refresh(document)
    stored = store_forms(
        session,
        document,
        new_forms,
        tax_year=year,
        user_id=document.user_id,
        file_sha256=document.file_sha256,
        first_ordinal=_ON_DEMAND_ORDINAL,
    )
    income_n, tax_n, merged_count = stored.income_n, stored.tax_n, 0
    for original, grown in zip(det_forms, merged[: len(det_forms)], strict=True):
        if grown is original:
            continue
        added_i, added_t = _append_sections(session, document, original, grown)
        if added_i or added_t:
            merged_count += 1
            income_n += added_i
            tax_n += added_t

    warnings = [check_warning(w) for w in result.warnings]
    if stored.inserted_forms or merged_count:
        warnings.append(REVIEW_WARNING)
    if stored.skipped and not stored.inserted_forms and not merged_count:
        warnings.append(
            f"{stored.skipped} form(s) read by the LLM were already stored and were skipped."
        )
    warnings.extend(stored.soft_warnings)
    old: list[str] = json.loads(document.warnings) if document.warnings else []
    combined = list(dict.fromkeys([*old, *warnings]))
    document.warnings = json.dumps(combined) if combined else None
    if stored.inserted_forms or merged_count:
        document.status = STATUS_PARSED
        document.error_detail = None
        document.tax_year = year
        if document.parser_id is None:
            document.parser_id = fallback.id
            document.parser_version = fallback.version
    session.commit()
    logger.info(
        "tax on-demand llm read: document_id=%s pages=%d requests=%d inserted=%d merged=%d "
        "skipped=%d",
        document.id,
        len(plan.pages),
        result.requests_sent,
        stored.inserted_forms,
        merged_count,
        stored.skipped,
    )
    return OnDemandResult(
        requests_sent=result.requests_sent,
        inserted_forms=stored.inserted_forms,
        skipped_duplicate_forms=stored.skipped,
        merged_forms=merged_count,
        income_item_count=income_n,
        tax_item_count=tax_n,
        warnings=tuple(dict.fromkeys(warnings)),
        pages=plan.pages,
    )
