"""Tax-form parser registry (plan 2.4), mirroring `ingest/registry.py`.

Discovers module-level `TaxFormParser` instances (conventionally ``PARSER``) in the modules of
`personal_finance_analyzer.ingest.tax.parsers` and picks the highest `detect()` score strictly above
`TAX_DETECT_THRESHOLD`. There is deliberately no generic fallback and no entry-point group in v1:
an unrecognised document raises `UnsupportedLayoutError`. This registry is independent of the
bank-statement registry, so tax parsers can never be selected for a statement (and vice versa).
"""

from __future__ import annotations

import importlib
import math
import pkgutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from personal_finance_analyzer.core.errors import UnsupportedLayoutError
from personal_finance_analyzer.core.logging import get_logger
from personal_finance_analyzer.core.tax_types import FormType, TaxFormParser
from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest.tax.year import detect_tax_year

logger = get_logger("ingest.tax.registry")

#: A parser must score strictly above this to be selected (same semantics as
#: `ingest.registry.DETECT_THRESHOLD`).
TAX_DETECT_THRESHOLD = 0.5


@dataclass(frozen=True, slots=True)
class FormClassification:
    """Result of scoring every tax parser against a document (for selection and diagnostics).

    `form_type` is set only when the best parser clears the threshold and declares exactly one
    form type (a parser covering several, e.g. 1099-NEC/MISC, leaves it `None`; `parse()` is
    authoritative). `scores` maps parser id to its `detect()` score.
    """

    form_type: FormType | None
    tax_year: int | None
    confidence: float
    scores: Mapping[str, float]
    parser_id: str | None = None


def _looks_like_tax_parser(obj: object) -> bool:
    return not isinstance(obj, type) and isinstance(obj, TaxFormParser)


def discover_tax_parsers() -> tuple[TaxFormParser, ...]:
    """Scan `personal_finance_analyzer.ingest.tax.parsers` for module-level `TaxFormParser` objects.

    Modules starting with ``_`` are skipped; a module that fails to import is skipped with a
    warning. Duplicate ids (e.g. a parser re-exported by another module) are kept once. The
    result is sorted by parser id for determinism.
    """
    import personal_finance_analyzer.ingest.tax.parsers as parsers_pkg

    found: dict[str, TaxFormParser] = {}
    for module_info in pkgutil.iter_modules(
        parsers_pkg.__path__, prefix=f"{parsers_pkg.__name__}."
    ):
        module_name = module_info.name.rsplit(".", 1)[-1]
        if module_name.startswith("_"):
            continue
        try:
            module = importlib.import_module(module_info.name)
        except Exception:  # one broken parser module must not disable the rest
            logger.warning("failed to import tax parser module %r", module_info.name, exc_info=True)
            continue
        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            obj = getattr(module, attr_name)
            if _looks_like_tax_parser(obj) and obj.id not in found:
                found[obj.id] = obj
    return tuple(found[key] for key in sorted(found))


def _safe_detect(parser: TaxFormParser, doc: ExtractedDoc) -> float:
    """`parser.detect(doc)` clamped to [0, 1]; an exception or non-finite value scores 0.0."""
    try:
        score = float(parser.detect(doc))
    except Exception:  # a raising detect() must not crash classification
        logger.warning("tax parser %r raised in detect(); scoring 0.0", parser.id, exc_info=True)
        return 0.0
    if not math.isfinite(score):
        return 0.0
    return min(1.0, max(0.0, score))


def classify_form(
    doc: ExtractedDoc, parsers: Sequence[TaxFormParser] | None = None
) -> FormClassification:
    """Score every parser (default `discover_tax_parsers()`) against `doc`.

    The best parser is the highest score (first in the given order on ties). `confidence` is that
    score even when it does not clear `TAX_DETECT_THRESHOLD`, in which case `form_type` and
    `parser_id` are `None`.
    """
    candidates = parsers if parsers is not None else discover_tax_parsers()
    scores: dict[str, float] = {}
    best: TaxFormParser | None = None
    best_score = 0.0
    for parser in candidates:
        score = _safe_detect(parser, doc)
        scores[parser.id] = score
        if best is None or score > best_score:
            best, best_score = parser, score
    selected = best is not None and best_score > TAX_DETECT_THRESHOLD
    form_type: FormType | None = None
    if selected and best is not None and len(best.form_types) == 1:
        form_type = best.form_types[0]
    return FormClassification(
        form_type=form_type,
        tax_year=detect_tax_year(doc),
        confidence=best_score,
        scores=MappingProxyType(scores),
        parser_id=best.id if selected and best is not None else None,
    )


def select_tax_parser(
    doc: ExtractedDoc, parsers: Sequence[TaxFormParser] | None = None
) -> TaxFormParser:
    """The parser with the highest score strictly above `TAX_DETECT_THRESHOLD`.

    Raises:
        UnsupportedLayoutError: no parser is confident. The message never contains document text.
    """
    candidates = parsers if parsers is not None else discover_tax_parsers()
    classification = classify_form(doc, candidates)
    if classification.parser_id is not None:
        for parser in candidates:
            if parser.id == classification.parser_id:
                return parser
    raise UnsupportedLayoutError(
        "This file is not a recognized tax form (supported: W-2, 1099-INT/DIV/B/NEC/MISC, "
        "consolidated 1099, Form 1040)."
    )
