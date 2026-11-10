"""Factory for the opt-in tax LLM fallback: the only module that imports `llm/tax_parser.py`.

Besides the import-time fallback it serves the per-document tools: a network-free preview of what
would be sent for chosen pages and an on-demand read of chosen pages (explicit per-document
consent, allowed whenever the LLM is configured, whatever the automatic setting says).

`build_tax_fallback` returns `None` (the deterministic-only path, nothing leaves the machine)
unless the LLM is enabled and configured AND the user switched on ``[ingest] tax_llm_fallback``.
The tax package itself imports no LLM code; it only receives the returned object through the
`TaxFallbackExtractor` protocol.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from personal_finance_analyzer.config import Settings, load_settings
from personal_finance_analyzer.core.errors import PfaError
from personal_finance_analyzer.core.tax_types import TaxFallbackExtractor
from personal_finance_analyzer.db.models import TaxDocument
from personal_finance_analyzer.db.pii_terms import user_pii_terms
from personal_finance_analyzer.ingest.tax import on_demand
from personal_finance_analyzer.llm.tax_parser import TaxLLMExtractor


def tax_fallback_enabled(settings: Settings) -> bool:
    """Is the fallback effective? (LLM configured and the separate tax opt-in on.)"""
    return settings.llm.is_configured and settings.ingest.tax_llm_fallback


def build_tax_fallback(
    session: Session, user_id: int | None, settings: Settings | None = None
) -> TaxFallbackExtractor | None:
    """The fallback extractor for ``user_id`` (their personal-info terms are redacted), or `None`.

    Call this before the import so the user's terms are read outside any write transaction.
    """
    resolved = settings if settings is not None else load_settings()
    if not tax_fallback_enabled(resolved):
        return None
    return TaxLLMExtractor(resolved.llm, user_pii_terms(session, resolved, user_id))


def build_on_demand_extractor(
    session: Session, user_id: int | None, settings: Settings | None = None
) -> TaxLLMExtractor | None:
    """The extractor for an explicit, user-initiated read of chosen pages, or `None` when the LLM
    is not configured. Unlike `build_tax_fallback` it ignores ``[ingest] tax_llm_fallback``: the
    user's click is the consent."""
    resolved = settings if settings is not None else load_settings()
    if not resolved.llm.is_configured:
        return None
    return TaxLLMExtractor(resolved.llm, user_pii_terms(session, resolved, user_id))


@dataclass(frozen=True, slots=True)
class TaxPreview:
    mode: Literal["none", "local", "remote"]
    resolved_url: str
    model: str
    pages: list[int]
    requests: list[str]
    total_chars: int
    redactions: int


def preview_pages(
    session: Session,
    document: TaxDocument,
    pdf_path: Path,
    pages: Sequence[int] | None,
    settings: Settings | None = None,
) -> TaxPreview:
    """Exactly the redacted text that would be sent for ``pages`` (``None`` = the pages the
    automatic fallback would send). No network call. Without a configured LLM nothing can be
    sent, so the preview is empty with ``mode`` ``none``."""
    resolved = settings if settings is not None else load_settings()
    cfg = resolved.llm
    if not cfg.is_configured:
        return TaxPreview("none", "", cfg.model, [], [], 0, 0)
    terms = user_pii_terms(session, resolved, document.user_id)
    plan = on_demand.load_plan(
        pdf_path, pii_terms=terms, document_year=document.tax_year, pages=pages
    )
    if pages is not None:
        present = {p.page_number for p in plan.extracted.pages}
        bad = sorted(set(pages) - present)
        if bad:
            raise PfaError(
                f"page(s) {', '.join(map(str, bad[:10]))} out of range "
                f"(this document has {len(present)} page(s))"
            )
    extractor = TaxLLMExtractor(cfg, terms)
    requests, _truncated = extractor.sendable_requests(plan.extracted, plan.regions)
    sent_pages = sorted({p for r in requests for p in r.pages})
    texts = [r.text for r in requests]
    return TaxPreview(
        mode=cfg.mode,
        resolved_url=cfg.resolved_url,
        model=cfg.model,
        pages=sent_pages,
        requests=texts,
        total_chars=sum(len(t) for t in texts),
        redactions=extractor.count_redactions(plan.extracted, sent_pages),
    )


def pii_terms_for(
    session: Session, user_id: int | None, settings: Settings | None = None
) -> tuple[str, ...]:
    """The personal-info terms redacted for ``user_id`` (config entries plus their aliases)."""
    resolved = settings if settings is not None else load_settings()
    return tuple(user_pii_terms(session, resolved, user_id))
