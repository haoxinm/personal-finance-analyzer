"""Merge the opt-in fallback extractor's forms into a deterministic parse (pure; no LLM import).

`apply_fallback` is called by the import pipeline after `orchestrator.parse_document` and before
any database write. The extractor is injected (`core.tax_types.TaxFallbackExtractor`); this module
never imports it, so the tax package stays free of LLM code.

Rules: deterministic results always win. The extractor is asked only about the regions the parsers
could not read (`ParsedTaxDocument.unparsed`, cropped to the rectangle they occupy), or, when
nothing parsed and a tax year is known, about the pages that show a W-2 / 1099 form heading (a
year alone is not enough: other documents are never sent). Its forms are appended, except that a
form the parser already has (same account / TIN, or mostly the same figures) is dropped and items
of a consolidated broker statement are merged into the matching deterministic account form for
sections the parser did not read. The extractor can never fail the import: any error
becomes a warning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from personal_finance_analyzer.core.errors import EgressViolation
from personal_finance_analyzer.core.logging import get_logger
from personal_finance_analyzer.core.tax_types import (
    ParsedIncomeItem,
    ParsedTaxDocument,
    ParsedTaxForm,
    ParsedTaxItem,
    TaxFallbackExtractor,
    UnparsedRegion,
)
from personal_finance_analyzer.core.types import ExtractedDoc, PageText
from personal_finance_analyzer.ingest.layout import cluster_rows
from personal_finance_analyzer.ingest.tax.common import MAX_TAX_YEAR, MIN_TAX_YEAR, check_warning
from personal_finance_analyzer.ingest.tax.normalize import normalize_doc
from personal_finance_analyzer.ingest.tax.orchestrator import ParseOutcome
from personal_finance_analyzer.ingest.tax.segment import find_anchors
from personal_finance_analyzer.ingest.tax.year import detect_tax_year

logger = get_logger("ingest.tax.fallback")

FALLBACK_FAILED_WARNING = "The LLM fallback failed unexpectedly; the file was imported without it."
REVIEW_WARNING = "Some values were read by your LLM; review them."


@dataclass(frozen=True, slots=True)
class FallbackRun:
    """The merged document (`None` when nothing could be read at all) plus bookkeeping."""

    document: ParsedTaxDocument | None
    requests_sent: int
    attempted: bool


#: A line that starts like a W-2 / 1099 form heading (for pages without word boxes).
_FORM_LINE = re.compile(
    r"^\s*(?:(?:20\d{2}\s+)?(?:Form\s*)?1099\s*-\s*[A-Z]{1,4}\b|(?:Form\s+)?W-?2\b.*"
    r"(?:Wage\s+and\s+Tax|Statement)|Wage\s+and\s+Tax\s+Statement|Consolidated\s+1099)",
    re.IGNORECASE | re.MULTILINE,
)

#: A line that starts like a Form 1040 heading (for pages without word boxes).
_FORM_1040_LINE = re.compile(
    r"^\s*(?:(?:20\d{2}\s+)?Form\s+1040(?:\s*-\s*SR)?\b|1040(?:\s*-\s*SR)?\s+U\.?S\.?\b"
    r"|U\.?S\.?\s+Individual\s+Income\s+Tax\s+Return)",
    re.IGNORECASE | re.MULTILINE,
)


def _page_evidence(page: PageText) -> str | None:
    """``"form"`` when `page` shows a W-2 / 1099 form heading, ``"other"`` when it shows another
    form (Form 1040), else `None`. A mention inside running text does not count."""
    if page.words:
        families = {a.family for a in find_anchors(cluster_rows(page.words), page.page_number)}
        if any(f == "w2" or f.startswith("1099") for f in families):
            return "form"
        if "1040" in families:
            return "other"
        return None
    if _FORM_LINE.search(page.text):
        return "form"
    return "other" if _FORM_1040_LINE.search(page.text) else None


def _whole_file_regions(doc: ExtractedDoc) -> tuple[UnparsedRegion, ...]:
    """The pages of a file nothing could be read from that may be sent: pages that show a W-2 /
    1099 form heading, plus the pages that continue them (no heading of their own, directly
    after). A user-supplied year is not evidence; a bank or card statement yields no region."""
    pages: list[int] = []
    inside = False
    for page in doc.pages:
        if not page.text.strip():
            continue
        evidence = _page_evidence(page)
        if evidence == "form":
            inside = True
        elif evidence == "other":
            inside = False
        if inside:
            pages.append(page.page_number)
    if not pages:
        return ()
    return (UnparsedRegion(kind="unknown", pages=tuple(pages), reason="unreadable"),)


def fallback_targets(
    extracted: ExtractedDoc, outcome: ParseOutcome | None, *, tax_year: int | None
) -> tuple[tuple[UnparsedRegion, ...], int | None]:
    """``(regions, year)`` the fallback would address: the deterministic result's unparsed regions
    (and its year), or, when nothing parsed, the W-2 / 1099 pages of the whole file (and the
    user's / detected year). Pure; the year is not range-checked."""
    det = outcome.document if outcome is not None else None
    if det is not None:
        return det.unparsed, det.tax_year
    regions = _whole_file_regions(extracted)
    year = tax_year if tax_year is not None else detect_tax_year(normalize_doc(extracted))
    return regions, year


def other_form_pages(doc: ExtractedDoc) -> frozenset[int]:
    """Pages that show another form (Form 1040) or continue one (pages after its heading, up to
    the next W-2 / 1099 heading). Such pages are never sent to the LLM."""
    pages: set[int] = set()
    inside = False
    for page in doc.pages:
        evidence = _page_evidence(page) if page.text.strip() else None
        if evidence == "other":
            inside = True
        elif evidence == "form":
            inside = False
        if inside:
            pages.add(page.page_number)
    return frozenset(pages)


def _same_account(llm: ParsedTaxForm, target: ParsedTaxForm) -> bool:
    for a, b in (
        (llm.account_last4, target.account_last4),
        (llm.payer_tin_last4, target.payer_tin_last4),
    ):
        if a is not None and b is not None and a == b:
            return True
    return False


def _merge_into(target: ParsedTaxForm, llm: ParsedTaxForm) -> ParsedTaxForm | None:
    """`target` plus the LLM items of sections the parser did not read; `None` if nothing new."""
    read_sections = {i.source_form for i in target.income} | {t.source_form for t in target.taxes}
    income: tuple[ParsedIncomeItem, ...] = tuple(
        i for i in llm.income if i.source_form not in read_sections
    )
    taxes: tuple[ParsedTaxItem, ...] = tuple(
        t for t in llm.taxes if t.source_form not in read_sections
    )
    if not income and not taxes:
        return None
    warnings = tuple(dict.fromkeys((*target.warnings, REVIEW_WARNING)))
    return replace(
        target,
        income=(*target.income, *income),
        taxes=(*target.taxes, *taxes),
        warnings=warnings,
    )


def _figures(form: ParsedTaxForm) -> set[tuple[str, int]]:
    return {(i.category, i.amount_minor) for i in form.income} | {
        (t.category, t.amount_minor) for t in form.taxes
    }


def _duplicates(llm: ParsedTaxForm, det: ParsedTaxForm) -> bool:
    """Is `llm` a re-read of the deterministic form `det`? Same form type, and the same account
    (or, without accounts, the same payer TIN), or at least half of its (category, amount) items
    already in `det`."""
    if llm.form_type != det.form_type:
        return False
    if llm.account_last4 is not None and det.account_last4 is not None:
        if llm.account_last4 == det.account_last4:
            return True
    elif _same_account(llm, det):
        return True
    mine = _figures(llm)
    theirs = _figures(det)
    return bool(mine) and len(mine & theirs) * 2 >= len(mine)


def _merge_forms(
    det: tuple[ParsedTaxForm, ...], llm_forms: tuple[ParsedTaxForm, ...]
) -> tuple[ParsedTaxForm, ...]:
    forms = list(det)
    consolidated = [i for i, f in enumerate(forms) if f.form_type == "1099-consolidated"]
    for llm in llm_forms:
        if llm.form_type == "1099-consolidated" and consolidated:
            matches = [i for i in consolidated if _same_account(llm, forms[i])]
            if not matches and len(consolidated) == 1:
                matches = consolidated
            if matches:
                merged = _merge_into(forms[matches[0]], llm)
                if merged is not None:
                    forms[matches[0]] = merged
                continue  # sections the parser read win; nothing is added as a second form
        if any(_duplicates(llm, d) for d in det):
            continue  # the model re-read a form the parser already has
        forms.append(llm)
    return tuple(forms)


def _sent_so_far(fallback: TaxFallbackExtractor) -> int:
    count = getattr(fallback, "requests_sent", 0)
    return count if isinstance(count, int) else 0


def apply_fallback(
    extracted: ExtractedDoc,
    outcome: ParseOutcome | None,
    fallback: TaxFallbackExtractor | None,
    *,
    tax_year: int | None,
) -> FallbackRun:
    """Run `fallback` over what the deterministic `outcome` could not read.

    Args:
        extracted: the file's text layer (original page numbers).
        outcome: the deterministic result, or `None` when parsing failed outright.
        fallback: the opt-in extractor, or `None` (then the outcome is returned unchanged).
        tax_year: the user's override, used only when no parsed document supplies a year.
    """
    det = outcome.document if outcome is not None else None
    if fallback is None or not extracted.has_text_layer:
        return FallbackRun(det, 0, False)
    regions, year = fallback_targets(extracted, outcome, tax_year=tax_year)
    if not regions or year is None or not MIN_TAX_YEAR <= year <= MAX_TAX_YEAR:
        return FallbackRun(det, 0, False)

    try:
        result = fallback.extract(extracted, regions, tax_year=year)
    except EgressViolation:
        raise
    except Exception as exc:  # the fallback must never fail an import
        logger.warning("tax llm fallback failed: %s", type(exc).__name__)
        sent = _sent_so_far(fallback)
        if det is None:
            return FallbackRun(None, sent, True)
        return FallbackRun(
            replace(det, warnings=(*det.warnings, check_warning(FALLBACK_FAILED_WARNING))),
            sent,
            True,
        )

    warnings = tuple(check_warning(w) for w in result.warnings)
    if det is None:
        if not result.forms:
            return FallbackRun(None, result.requests_sent, True)
        document = ParsedTaxDocument(
            tax_year=year, forms=result.forms, warnings=tuple(dict.fromkeys(warnings))
        )
        return FallbackRun(document, result.requests_sent, True)
    forms = _merge_forms(det.forms, result.forms)
    merged = replace(det, forms=forms, warnings=tuple(dict.fromkeys((*det.warnings, *warnings))))
    return FallbackRun(merged, result.requests_sent, True)
