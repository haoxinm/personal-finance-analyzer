"""Parse a whole tax file: normalise, segment, classify each segment, collect the results.

`parse_document(doc, tax_year=...)` replaces "pick ONE parser for the file". The document is
normalised (`normalize.normalize_doc`) and split into segments (`segment.build_segments`):
consecutive regions of one form family. A file with a single segment takes exactly the path it
always did (`select_tax_parser` + `parser.parse`). A file with several segments (a mixed package: a
W-2 page, a 1099-INT page and a 1099-DIV page; or a 1040 with attached W-2s) classifies and parses
each segment with the best parser for it; runs of adjacent 1099 sections that a consolidated
broker statement owns (its marker is present) are kept together and parsed as one document.

Partial success: a segment that no parser recognises, or whose parser raises `ParserError`, becomes
a warning plus an `UnparsedRegion` (original page numbers, fixed generic reason) while the other
segments' forms are returned. Only when nothing at all could be read does the call raise
(`UnsupportedLayoutError` if no segment was recognised, else the first `ParserError`).

Forms whose tax year differs from the file's majority year are dropped with a warning.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, replace

from personal_finance_analyzer.core.errors import ParserError, UnsupportedLayoutError
from personal_finance_analyzer.core.tax_types import (
    PageBox,
    ParsedTaxDocument,
    ParsedTaxForm,
    TaxFormParser,
    UnparsedRegion,
)
from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest.layout import parse_money
from personal_finance_analyzer.ingest.tax.common import check_warning
from personal_finance_analyzer.ingest.tax.normalize import normalize_doc
from personal_finance_analyzer.ingest.tax.parsers.f1099_consolidated import PARSER as CONSOLIDATED
from personal_finance_analyzer.ingest.tax.registry import (
    discover_tax_parsers,
    select_tax_parser,
)
from personal_finance_analyzer.ingest.tax.segment import Segment, build_segments
from personal_finance_analyzer.ingest.tax.year import detect_tax_year

#: A consolidated-statement score at or above this keeps adjacent 1099 sections together.
_CONSOLIDATED_KEEP = 0.9
_UNRECOGNIZED_WARNING = (
    "A section that looks like a tax form could not be recognized and was skipped."
)
_OTHER_YEAR_WARNING = "A form for a different tax year was skipped."


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """The parsed document plus which parsers produced it (``("multi",)``-style handling is the
    caller's: see `parser_id`)."""

    document: ParsedTaxDocument
    parser_ids: tuple[str, ...]
    parser_versions: tuple[str, ...]

    @property
    def parser_id(self) -> str:
        return self.parser_ids[0] if len(self.parser_ids) == 1 else "multi"

    @property
    def parser_version(self) -> str:
        return self.parser_versions[0] if len(self.parser_versions) == 1 else "multi"


def _has_amounts(doc: ExtractedDoc) -> bool:
    return any(
        parse_money(w.text) is not None and ("." in w.text or "," in w.text or "$" in w.text)
        for page in doc.pages
        for w in page.words
    )


def _merged(segments: list[Segment]) -> Segment:
    pages = tuple(p for s in segments for p in s.doc.pages)
    page_map: dict[int, int] = {}
    for s in segments:
        page_map.update(s.page_map)
    first = segments[0].doc
    return Segment(
        family="1099",
        doc=ExtractedDoc(
            file_sha256=first.file_sha256,
            page_count=len(pages),
            pages=pages,
            has_text_layer=first.has_text_layer,
        ),
        page_map=page_map,
        anchors=tuple(a for s in segments for a in s.anchors),
    )


def _coalesce_consolidated(segments: list[Segment]) -> list[Segment]:
    """Merge each run of adjacent ``1099-*`` segments of different sections into one segment when
    a consolidated statement claims it (a run of one stays as it is)."""
    out: list[Segment] = []
    run: list[Segment] = []

    def flush() -> None:
        if len(run) > 1:
            candidate = _merged(run)
            if CONSOLIDATED.detect(candidate.doc) >= _CONSOLIDATED_KEEP:
                out.append(candidate)
                run.clear()
                return
        out.extend(run)
        run.clear()

    for segment in segments:
        if segment.family is not None and segment.family.startswith("1099"):
            run.append(segment)
        else:
            flush()
            out.append(segment)
    flush()
    return out


def _boxes(segment: Segment, pages: Collection[int]) -> tuple[PageBox, ...]:
    """One rectangle per original page: the extent of the words of the segment pages `pages`
    (segment page numbers). Pages without words get no box (the whole page is meant)."""
    extents: dict[int, tuple[float, float, float, float]] = {}
    for page in segment.doc.pages:
        if page.page_number not in pages or not page.words:
            continue
        original = segment.page_map.get(page.page_number, page.page_number)
        x0 = min(w.x0 for w in page.words)
        top = min(w.top for w in page.words)
        x1 = max(w.x1 for w in page.words)
        bottom = max(w.bottom for w in page.words)
        old = extents.get(original)
        if old is not None:
            x0, top = min(x0, old[0]), min(top, old[1])
            x1, bottom = max(x1, old[2]), max(bottom, old[3])
        extents[original] = (x0, top, x1, bottom)
    return tuple((n, *box) for n, box in sorted(extents.items()))


def _remap(region: UnparsedRegion, segment: Segment) -> UnparsedRegion:
    pages = tuple(sorted({segment.page_map.get(p, p) for p in region.pages}))
    boxes = _boxes(segment, set(region.pages))
    covered = {b[0] for b in boxes}
    if covered != set(pages):
        boxes = ()  # a page without words: send it whole
    return replace(region, pages=pages, boxes=boxes)


def _segment_region(segment: Segment, reason: str) -> UnparsedRegion:
    pages = segment.original_pages
    boxes = _boxes(segment, set(segment.page_map))
    if {b[0] for b in boxes} != set(pages):
        boxes = ()
    return UnparsedRegion(_kind(segment), pages, reason, boxes)


def _kind(segment: Segment) -> str:
    return segment.family or "unknown"


def _parse_single(
    segment: Segment, tax_year: int | None
) -> tuple[ParsedTaxDocument, TaxFormParser]:
    parser = select_tax_parser(segment.doc)
    parsed = parser.parse(segment.doc, tax_year=tax_year)
    remapped = tuple(_remap(r, segment) for r in parsed.unparsed)
    return replace(parsed, unparsed=remapped), parser


def parse_document(doc: ExtractedDoc, *, tax_year: int | None = None) -> ParseOutcome:
    """Parse `doc` (see the module docstring).

    Raises:
        UnsupportedLayoutError: nothing in the file is a recognised tax form.
        ParserError: forms were recognised but none could be read (generic message).
    """
    normalized = normalize_doc(doc)
    segments = build_segments(normalized)
    segments = _coalesce_consolidated(segments)
    if len(segments) <= 1:
        only = segments[0] if segments else Segment(None, normalized, {}, ())
        parsed, parser = _parse_single(only, tax_year)
        return ParseOutcome(parsed, (parser.id,), (parser.version,))

    parsers = discover_tax_parsers()
    file_year = tax_year if tax_year is not None else detect_tax_year(normalized)
    forms: list[ParsedTaxForm] = []
    warnings: list[str] = []
    unparsed: list[UnparsedRegion] = []
    used: dict[str, str] = {}
    first_error: ParserError | None = None
    for segment in segments:
        year = tax_year if tax_year is not None else detect_tax_year(segment.doc) or file_year
        try:
            parser = select_tax_parser(segment.doc, parsers)
        except UnsupportedLayoutError:
            if segment.anchors and _has_amounts(segment.doc):
                warnings.append(_UNRECOGNIZED_WARNING)
                unparsed.append(_segment_region(segment, "unrecognized"))
            continue
        try:
            parsed = parser.parse(segment.doc, tax_year=year)
        except ParserError as exc:
            first_error = first_error or exc
            warnings.append(
                check_warning(f"{str(exc).rstrip('.')}; that part of the file was skipped.")
            )
            unparsed.append(_segment_region(segment, "unreadable"))
            continue
        used[parser.id] = parser.version
        forms.extend(parsed.forms)
        warnings.extend(parsed.warnings)
        unparsed.extend(_remap(r, segment) for r in parsed.unparsed)

    if not forms:
        if first_error is not None:
            raise first_error
        raise UnsupportedLayoutError(
            "This file is not a recognized tax form (supported: W-2, 1099-INT/DIV/B/NEC/MISC, "
            "consolidated 1099, Form 1040)."
        )
    years = Counter(f.tax_year for f in forms)
    year = tax_year if tax_year is not None else years.most_common(1)[0][0]
    kept = [f for f in forms if f.tax_year == year]
    if len(kept) != len(forms):
        warnings.append(_OTHER_YEAR_WARNING)
    document = ParsedTaxDocument(
        tax_year=year,
        forms=tuple(kept),
        warnings=tuple(dict.fromkeys(warnings)),
        unparsed=tuple(unparsed),
    )
    ids = tuple(sorted(used))
    return ParseOutcome(document, ids, tuple(used[i] for i in ids))
