"""Page-selection helpers: parsing a user-typed page spec such as ``"1-3,5"``, and validating a
selection against a document's page count. No I/O."""

from __future__ import annotations

import json
from collections.abc import Sequence

from personal_finance_analyzer.core.errors import PfaError

#: Upper bound on any page number when the caller does not know the page count: a typed range
#: is never expanded past it (``1-999999999999`` must not build a huge list).
MAX_SPEC_PAGE = 100_000


def parse_page_spec(spec: str, max_page: int | None = None) -> list[int]:
    """Parse ``"1-3,5"`` into ``[1, 2, 3, 5]`` (sorted, de-duplicated).

    ``max_page`` (the document's page count, when known) bounds every page *before* a range is
    expanded; without it the ceiling is `MAX_SPEC_PAGE`.

    Raises:
        ValueError: empty spec, a non-numeric token, a page < 1, a reversed range, or a page
            beyond the bound.
    """
    limit = min(max_page, MAX_SPEC_PAGE) if max_page is not None else MAX_SPEC_PAGE
    pages: set[int] = set()
    tokens = [t.strip() for t in spec.split(",")]
    if not spec.strip() or any(not t for t in tokens):
        raise ValueError(f"invalid page spec {spec!r}: expected something like '1-3,5'")
    for token in tokens:
        lo_s, sep, hi_s = token.partition("-")
        try:
            lo = int(lo_s.strip())
            hi = int(hi_s.strip()) if sep else lo
        except ValueError:
            raise ValueError(
                f"invalid page spec {spec!r}: {token!r} is not a page or range"
            ) from None
        if lo < 1 or hi < lo:
            raise ValueError(f"invalid page spec {spec!r}: bad range {token!r}")
        if hi > limit:
            raise ValueError(
                f"invalid page spec {spec!r}: {token!r} is out of range (pages 1-{limit} exist)"
            )
        pages.update(range(lo, hi + 1))
    return sorted(pages)


def validate_pages(pages: Sequence[int], page_count: int) -> list[int]:
    """Dedupe, sort and range-check ``pages`` against ``1..page_count``.

    Raises:
        PfaError: the selection is empty or names a page outside the document.
    """
    unique = sorted(set(pages))
    if not unique:
        raise PfaError("no pages selected")
    bad = [p for p in unique if p < 1 or p > page_count]
    if bad:
        raise PfaError(
            f"page(s) {', '.join(map(str, bad))} out of range (this statement has "
            f"{page_count} page(s))"
        )
    return unique


def dump_pages(pages: Sequence[int] | None) -> str | None:
    return json.dumps(list(pages)) if pages else None


def load_pages(raw: str | None) -> list[int] | None:
    """Decode `statements.selected_pages`; ``None`` for unset or malformed."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, list) or not all(isinstance(v, int) for v in value):
        return None
    return value or None
