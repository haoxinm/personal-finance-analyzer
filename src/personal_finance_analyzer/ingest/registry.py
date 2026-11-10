"""Parser registry (P1-B).

Discovers every built-in `StatementParser` under `personal_finance_analyzer.ingest.parsers` (one layout per
module: `layout_a_credit`, `layout_b_credit`, `layout_c_credit`, `layout_d_bank`, ...) plus any
third-party parser registered under the ``personal_finance_analyzer.parsers`` entry-point group, so an
external package can add issuer support without a PR to this repository.

`select()` picks the highest `detect()` score above ``0.5``; otherwise it falls back to the
generic parser (`personal_finance_analyzer.ingest.parsers.generic_table.generic_table`), which is never part
of the scored candidate pool — it is the guaranteed default, not a competitor (§2d.2, D3).
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Sequence
from importlib.metadata import entry_points

from personal_finance_analyzer.core.logging import get_logger
from personal_finance_analyzer.core.types import ExtractedDoc, StatementParser
from personal_finance_analyzer.ingest.parsers.generic_table import generic_table

logger = get_logger("ingest.registry")

#: Entry-point group third-party packages register parsers under (`pyproject.toml`
#: ``[project.entry-points."personal_finance_analyzer.parsers"]``).
ENTRY_POINT_GROUP = "personal_finance_analyzer.parsers"

#: A parser must score strictly above this to be selected over the generic fallback.
DETECT_THRESHOLD = 0.5

#: Modules under `ingest.parsers` that are not scored layout parsers, so they never compete with
#: the layout parsers for `select()` and are never double-counted as both "built-in" and
#: "fallback".
_EXCLUDED_MODULES = frozenset({"generic_table"})


def discover_builtin_parsers() -> tuple[StatementParser, ...]:
    """Scan `personal_finance_analyzer.ingest.parsers` for module-level objects implementing
    `StatementParser`, excluding the generic fallback module itself.

    A module that fails to import is skipped with a warning rather than crashing discovery for
    every other parser.
    """
    import personal_finance_analyzer.ingest.parsers as parsers_pkg

    found: list[StatementParser] = []
    for module_info in pkgutil.iter_modules(
        parsers_pkg.__path__, prefix=f"{parsers_pkg.__name__}."
    ):
        module_name = module_info.name.rsplit(".", 1)[-1]
        if module_name in _EXCLUDED_MODULES or module_name.startswith("_"):
            continue
        try:
            module = importlib.import_module(module_info.name)
        except Exception:  # one broken layout module must not disable the rest
            logger.warning("failed to import parser module %r", module_info.name, exc_info=True)
            continue
        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            obj = getattr(module, attr_name)
            if _looks_like_parser(obj):
                found.append(obj)
    return tuple(found)


def discover_entrypoint_parsers() -> tuple[StatementParser, ...]:
    """Load third-party parsers registered under the ``personal_finance_analyzer.parsers`` entry-point
    group. A broken or non-conforming entry point is skipped with a warning, never raised."""
    found: list[StatementParser] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
        except Exception:  # a broken third-party entry point must not crash startup
            logger.warning("failed to load parser entry point %r", ep.name, exc_info=True)
            continue
        if _looks_like_parser(obj):
            found.append(obj)
        else:
            logger.warning(
                "entry point %r (%s) does not implement StatementParser", ep.name, ep.value
            )
    return tuple(found)


def discover_all_parsers() -> tuple[StatementParser, ...]:
    """Every built-in plus every third-party parser, in that order. Does not include the generic
    fallback (see module docstring)."""
    return discover_builtin_parsers() + discover_entrypoint_parsers()


def _looks_like_parser(obj: object) -> bool:
    return not isinstance(obj, type) and isinstance(obj, StatementParser)


def _safe_detect(parser: StatementParser, doc: ExtractedDoc) -> float:
    """Call `parser.detect(doc)`, treating an exception as a score of ``0.0`` (§3.1: `detect()`
    must not raise, but the registry does not trust that promise) and logging a warning."""
    try:
        return parser.detect(doc)
    except Exception:  # a raising detect() must not crash selection
        logger.warning("parser %r raised in detect(); scoring 0.0", parser.id, exc_info=True)
        return 0.0


def select(doc: ExtractedDoc, parsers: Sequence[StatementParser] | None = None) -> StatementParser:
    """Select the parser for ``doc``.

    Args:
        doc: the extracted document to score parsers against.
        parsers: candidate parsers to score, defaulting to `discover_all_parsers()`. The generic
            fallback is never part of this pool and is not accepted here — it is always the
            result when no candidate scores above `DETECT_THRESHOLD`.

    Returns:
        The candidate with the highest `detect()` score, if that score is strictly greater than
        `DETECT_THRESHOLD`; otherwise the generic fallback parser. A candidate whose `detect()`
        raises is scored ``0.0`` rather than crashing selection.
    """
    candidates = parsers if parsers is not None else discover_all_parsers()

    best: StatementParser | None = None
    best_score = 0.0
    for parser in candidates:
        score = _safe_detect(parser, doc)
        if best is None or score > best_score:
            best, best_score = parser, score

    if best is not None and best_score > DETECT_THRESHOLD:
        return best
    return generic_table
