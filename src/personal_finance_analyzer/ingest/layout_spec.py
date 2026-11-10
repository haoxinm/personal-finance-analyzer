"""Declarative layout spec format and interpreter (§2c intro, §2d.2, §2e.2, §2f.1, P1-H, A21).

A layout spec is **data, not code** — no expressions, no callables, no arbitrary execution. It
describes column x-bands, section headings, sign conventions and year inference declaratively, in
YAML, and is validated against `layout_spec.schema.json` before it is ever run. `SpecParser`
interprets a validated spec and implements the frozen `StatementParser` protocol (§3.1), so a
spec-driven parser is indistinguishable to the registry from a hand-written one.

**Versioning (A21).** A spec is an immutable ``(name, version)`` row: `revise()` inserts
``version + 1`` for a name and never mutates an existing row. `resolve_spec()` picks the highest
``version`` with ``approved = 1`` for an issuer and account type.

**Pasted specs (§2e.2, §2d.2).** `load_spec(text, source=...)` is the single entry point for every
spec regardless of origin — a user-pasted spec and an LLM-proposed one run identical validation and
both come back with ``approved=False``. No origin is trusted more than another.

Regex fields (section headings, terminators, excluded-table headings) are compiled with a length
cap and a catastrophic-backtracking heuristic at load time (§6.4) — a spec is untrusted input even
when a person approved it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from personal_finance_analyzer.core.errors import ParserError, PfaError
from personal_finance_analyzer.core.types import (
    AccountHint,
    ExtractedDoc,
    KindHint,
    ParsedStatement,
    RawTransaction,
)
from personal_finance_analyzer.db.models import LayoutSpec
from personal_finance_analyzer.ingest.layout import (
    Band,
    assign_to_bands,
    cluster_rows,
    extract_period_and_balances,
    infer_year,
    parse_date,
    parse_money,
)

_SCHEMA_PATH = Path(__file__).with_name("layout_spec.schema.json")
_SCHEMA: dict[str, Any] = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))

#: Regex fields are capped at this many characters; also enforced in the JSON Schema, but checked
#: again here so a schema edit can never silently drop the guard (§6.4).
MAX_PATTERN_LENGTH = 200

#: Heuristic for catastrophic backtracking: a group containing a quantifier, itself immediately
#: quantified — e.g. ``(a+)+``, ``(\\d*)*``, ``(ab+)+``. Not a complete detector, but it catches
#: the classic shape and needs no execution to check.
_NESTED_QUANTIFIER_RE = re.compile(r"\([^()]*[+*][^()]*\)[+*]")

_SourceLiteral = Literal["pasted", "llm_proposed", "user_authored"]
_ColumnType = Literal["date", "text", "money", "balance"]
_MoneyRole = Literal["debit", "credit", "amount"]
_SectionsMode = Literal["heading", "column_header_row"]
_SignConvention = Literal["unsigned", "leading_minus"]
_YearInference = Literal["from_period", "explicit"]


# ------------------------------------------------------------------------------------------------
# Errors
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpecFieldError:
    """One field-level problem with a layout spec, e.g. ``columns[2].x1``."""

    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.field}: {self.message}"


class LayoutSpecError(PfaError):
    """A layout spec is invalid. Carries every field-level error found — a spec is rejected as a
    whole, never partially applied (§2c)."""

    def __init__(self, message: str, errors: tuple[SpecFieldError, ...] = ()) -> None:
        self.errors = errors
        detail = "; ".join(str(e) for e in errors)
        super().__init__(f"{message}: {detail}" if detail else message)


# ------------------------------------------------------------------------------------------------
# Minimal JSON Schema (draft-07 subset) validator
#
# Deliberately not the `jsonschema` package: pyproject.toml's dependency set is frozen by §0.3 and
# does not list it. The subset below (type, enum, required, properties, additionalProperties,
# items, minItems, minimum, maximum, minLength, maxLength, pattern) is exactly what
# `layout_spec.schema.json` uses, and is itself a valid JSON Schema document that a full validator
# could also read.
# ------------------------------------------------------------------------------------------------


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return True  # pragma: no cover - schema only uses the types above


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def validate_against_schema(
    data: Any, schema: Mapping[str, Any], *, path: str = ""
) -> list[SpecFieldError]:
    """Validate ``data`` against ``schema`` using the draft-07 subset documented above.

    Returns every field-level error found (empty when valid); never raises on invalid *data*
    (only on a malformed *schema*, which would be a bug in this module, not in a spec).
    """
    errors: list[SpecFieldError] = []
    label = path or "<root>"

    if "enum" in schema and data not in schema["enum"]:
        return [SpecFieldError(label, f"must be one of {schema['enum']!r}, got {data!r}")]

    expected_type = schema.get("type")
    if expected_type is not None and not _type_matches(data, expected_type):
        return [
            SpecFieldError(label, f"expected type {expected_type!r}, got {type(data).__name__}")
        ]

    if isinstance(data, dict) and expected_type in (None, "object"):
        for key in schema.get("required", ()):
            if key not in data:
                errors.append(SpecFieldError(_join(path, key), "is required"))
        properties: Mapping[str, Any] = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in data.items():
            if key in properties:
                errors.extend(
                    validate_against_schema(value, properties[key], path=_join(path, key))
                )
            elif additional is False:
                errors.append(SpecFieldError(_join(path, key), "unexpected key"))
    elif isinstance(data, list) and expected_type in (None, "array"):
        min_items = schema.get("minItems")
        if min_items is not None and len(data) < min_items:
            errors.append(SpecFieldError(label, f"must have at least {min_items} item(s)"))
        item_schema = schema.get("items")
        if item_schema is not None:
            for i, item in enumerate(data):
                errors.extend(validate_against_schema(item, item_schema, path=f"{path}[{i}]"))
    elif isinstance(data, str) and expected_type in (None, "string"):
        min_length, max_length = schema.get("minLength"), schema.get("maxLength")
        pattern = schema.get("pattern")
        if min_length is not None and len(data) < min_length:
            errors.append(SpecFieldError(label, f"must be at least {min_length} character(s)"))
        if max_length is not None and len(data) > max_length:
            errors.append(SpecFieldError(label, f"must be at most {max_length} character(s)"))
        if pattern is not None and re.search(pattern, data) is None:
            errors.append(SpecFieldError(label, f"must match pattern {pattern!r}"))
    elif (
        isinstance(data, int | float)
        and not isinstance(data, bool)
        and expected_type in (None, "number", "integer")
    ):
        minimum, maximum = schema.get("minimum"), schema.get("maximum")
        if minimum is not None and data < minimum:
            errors.append(SpecFieldError(label, f"must be >= {minimum}"))
        if maximum is not None and data > maximum:
            errors.append(SpecFieldError(label, f"must be <= {maximum}"))

    return errors


def _compile_pattern_safe(text: str, field_path: str) -> re.Pattern[str]:
    """Compile ``text`` as a case-insensitive regex, rejecting anything too long or shaped for
    catastrophic backtracking (§6.4) — a spec is untrusted input even when approved."""
    if len(text) > MAX_PATTERN_LENGTH:
        raise LayoutSpecError(
            "layout spec regex is too long",
            (SpecFieldError(field_path, f"exceeds {MAX_PATTERN_LENGTH} characters"),),
        )
    if _NESTED_QUANTIFIER_RE.search(text):
        raise LayoutSpecError(
            "layout spec regex may backtrack catastrophically",
            (SpecFieldError(field_path, "nested quantifiers (e.g. '(a+)+') are not allowed"),),
        )
    try:
        return re.compile(text, re.IGNORECASE)
    except re.error as exc:
        raise LayoutSpecError(
            "layout spec regex is invalid", (SpecFieldError(field_path, str(exc)),)
        ) from exc


# ------------------------------------------------------------------------------------------------
# Parsed spec structure
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetectSpec:
    all_of: tuple[str, ...]
    any_of: tuple[str, ...]
    score: float


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    name: str
    x0: float
    x1: float
    type: _ColumnType
    formats: tuple[str, ...] = ()
    optional: bool = False
    multiline: bool = False
    role: _MoneyRole | None = None  # money columns only; default is "amount"


@dataclass(frozen=True, slots=True)
class SectionPattern:
    """A section heading pattern. ``kind_hint`` is the fallback used for both flow directions;
    ``outflow_kind_hint``/``inflow_kind_hint`` override it for a row whose resolved flow (I5) is
    that direction — e.g. in Layout A/B, ``PURCHASES`` means `purchase` for an outflow row but
    `adjustment` for the rare inflow row (a merchant-issued statement credit)."""

    match: re.Pattern[str]
    match_text: str
    kind_hint: KindHint | None = None
    outflow_kind_hint: KindHint | None = None
    inflow_kind_hint: KindHint | None = None


@dataclass(frozen=True, slots=True)
class SectionsSpec:
    mode: _SectionsMode
    patterns: tuple[SectionPattern, ...]
    terminator: re.Pattern[str] | None
    terminator_text: str | None
    exclude_tables: tuple[re.Pattern[str], ...]
    exclude_tables_text: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SignSpec:
    outflow: _SignConvention
    inflow: _SignConvention


@dataclass(frozen=True, slots=True)
class TotalsSpec:
    section_totals: bool


@dataclass(frozen=True, slots=True)
class FxSpec:
    """A dateless continuation line printing the original foreign-currency amount and exchange
    rate (§2c), e.g. ``FOREIGN CURRENCY AMOUNT 35.00 GBP EXCH RATE 1.20000``. A match sets the
    *previous* transaction's `fx_amount_minor`/`fx_currency`/`fx_rate` and never touches its
    description. Groups are referenced by name or 1-based index, exactly as `re.Match.group`
    takes them."""

    pattern: re.Pattern[str]
    amount_group: str | int
    currency_group: str | int
    rate_group: str | int


@dataclass(frozen=True, slots=True)
class LayoutSpecDoc:
    """A fully validated, parsed layout spec — the interpreter's input."""

    id: str
    version: int
    account_type: Literal["credit", "checking", "savings"]
    currency: str
    detect: DetectSpec
    columns: tuple[ColumnSpec, ...]
    sections: SectionsSpec | None
    sign: SignSpec
    year_inference: _YearInference
    totals: TotalsSpec
    #: LOCAL ONLY (I1b): last-4 account mask, extracted from the document's full text. `None`
    #: when the spec declares no `account_mask` block.
    account_mask_pattern: re.Pattern[str] | None
    #: `None` when the spec declares no `fx` block (most layouts never print a foreign-currency
    #: continuation line).
    fx: FxSpec | None


def _build_doc(data: Mapping[str, Any]) -> LayoutSpecDoc:
    """Turn already schema-valid ``data`` into a `LayoutSpecDoc`, applying the semantic checks the
    JSON Schema cannot express (column ordering, required roles, regex safety) and compiling every
    regex field. Raises `LayoutSpecError` with every problem found, never just the first."""
    errors: list[SpecFieldError] = []
    columns: list[ColumnSpec] = []
    seen_names: set[str] = set()

    for i, col in enumerate(data["columns"]):
        path = f"columns[{i}]"
        name = col["name"]
        if name in seen_names:
            errors.append(SpecFieldError(f"{path}.name", f"duplicate column name {name!r}"))
        seen_names.add(name)

        x0, x1 = float(col["x0"]), float(col["x1"])
        if x0 >= x1:
            errors.append(SpecFieldError(path, f"x0 ({x0}) must be less than x1 ({x1})"))

        col_type: _ColumnType = col["type"]
        formats = tuple(col.get("formats", ()))
        if col_type == "date" and not formats:
            errors.append(
                SpecFieldError(f"{path}.formats", "date columns require at least one format")
            )

        role = col.get("role")
        if role is not None and col_type != "money":
            errors.append(SpecFieldError(f"{path}.role", "role is only valid on money columns"))

        columns.append(
            ColumnSpec(
                name=name,
                x0=x0,
                x1=x1,
                type=col_type,
                formats=formats,
                optional=bool(col.get("optional", False)),
                multiline=bool(col.get("multiline", False)),
                role=role,
            )
        )

    names = {c.name for c in columns}
    if "posted_date" not in names:
        errors.append(SpecFieldError("columns", "a 'posted_date' column is required"))
    if "description" not in names:
        errors.append(SpecFieldError("columns", "a 'description' column is required"))

    money_cols = [c for c in columns if c.type == "money"]
    roles = {c.role or "amount" for c in money_cols}
    if not money_cols:
        errors.append(SpecFieldError("columns", "at least one money column is required"))
    elif "amount" in roles and ("debit" in roles or "credit" in roles):
        errors.append(
            SpecFieldError("columns", "cannot mix an 'amount' column with debit/credit columns")
        )

    sections_data = data.get("sections")
    if sections_data is not None:
        for i, p in enumerate(sections_data.get("patterns", ())):
            if not any(k in p for k in ("kind_hint", "outflow_kind_hint", "inflow_kind_hint")):
                errors.append(
                    SpecFieldError(
                        f"sections.patterns[{i}]",
                        "must set kind_hint, outflow_kind_hint, or inflow_kind_hint",
                    )
                )

    fx_data = data.get("fx")
    if fx_data is not None:
        for group_field in ("amount_group", "currency_group", "rate_group"):
            value = fx_data.get(group_field)
            is_valid_index = isinstance(value, int) and not isinstance(value, bool) and value >= 1
            if not (isinstance(value, str) and value) and not is_valid_index:
                errors.append(
                    SpecFieldError(
                        f"fx.{group_field}",
                        "must be a non-empty group name or a 1-based group index",
                    )
                )

    if errors:
        raise LayoutSpecError("layout spec has field-level errors", tuple(errors))

    detect_data = data["detect"]
    detect = DetectSpec(
        all_of=tuple(detect_data.get("all_of", ())),
        any_of=tuple(detect_data.get("any_of", ())),
        score=float(detect_data["score"]),
    )

    sections: SectionsSpec | None = None
    if sections_data is not None:
        patterns = tuple(
            SectionPattern(
                match=_compile_pattern_safe(p["match"], f"sections.patterns[{i}].match"),
                match_text=p["match"],
                kind_hint=p.get("kind_hint"),
                outflow_kind_hint=p.get("outflow_kind_hint"),
                inflow_kind_hint=p.get("inflow_kind_hint"),
            )
            for i, p in enumerate(sections_data.get("patterns", ()))
        )
        terminator_text = sections_data.get("terminator")
        terminator = (
            _compile_pattern_safe(terminator_text, "sections.terminator")
            if terminator_text
            else None
        )
        exclude_texts = tuple(sections_data.get("exclude_tables", ()))
        exclude_patterns = tuple(
            _compile_pattern_safe(t, f"sections.exclude_tables[{i}]")
            for i, t in enumerate(exclude_texts)
        )
        sections = SectionsSpec(
            mode=sections_data.get("mode", "heading"),
            patterns=patterns,
            terminator=terminator,
            terminator_text=terminator_text,
            exclude_tables=exclude_patterns,
            exclude_tables_text=exclude_texts,
        )

    sign_data = data.get("sign", {})
    sign = SignSpec(
        outflow=sign_data.get("outflow", "leading_minus"),
        inflow=sign_data.get("inflow", "leading_minus"),
    )

    totals_data = data.get("totals", {})
    totals = TotalsSpec(section_totals=bool(totals_data.get("section_totals", False)))

    account_mask_data = data.get("account_mask")
    account_mask_pattern = (
        _compile_pattern_safe(account_mask_data["pattern"], "account_mask.pattern")
        if account_mask_data is not None
        else None
    )

    fx: FxSpec | None = None
    if fx_data is not None:
        fx = FxSpec(
            pattern=_compile_pattern_safe(fx_data["pattern"], "fx.pattern"),
            amount_group=fx_data["amount_group"],
            currency_group=fx_data["currency_group"],
            rate_group=fx_data["rate_group"],
        )

    return LayoutSpecDoc(
        id=data["id"],
        version=int(data["version"]),
        account_type=data["account_type"],
        currency=data.get("currency", "USD"),
        detect=detect,
        columns=tuple(columns),
        sections=sections,
        sign=sign,
        year_inference=data.get("year_inference", "from_period"),
        totals=totals,
        account_mask_pattern=account_mask_pattern,
        fx=fx,
    )


# ------------------------------------------------------------------------------------------------
# Interpreter
# ------------------------------------------------------------------------------------------------


_Flow = Literal["outflow", "inflow"]


def _resolve_amount_and_flow(
    band_values: Mapping[str, str],
    money_cols: Sequence[ColumnSpec],
    sign: SignSpec,
) -> tuple[int, _Flow] | None:
    """Resolve one row's signed `amount_minor` (I5) and flow direction from its money column(s).

    A debit/credit column pair (Layout D, A26) determines the flow directly: a filled `debit`
    cell is an outflow, a filled `credit` cell is an inflow. A single `amount` column determines
    it from the printed sign, per `sign` — whichever of `outflow`/`inflow` is `leading_minus` is
    the direction the printed sign confirms; when both conventions are the same the print carries
    no directional information and outflow is assumed (a wrong guess here is caught downstream by
    reconciliation, A25).
    """
    debit_col = next((c for c in money_cols if c.role == "debit"), None)
    credit_col = next((c for c in money_cols if c.role == "credit"), None)
    if debit_col is not None or credit_col is not None:
        debit_text = band_values.get(debit_col.name, "") if debit_col is not None else ""
        credit_text = band_values.get(credit_col.name, "") if credit_col is not None else ""
        debit = parse_money(debit_text) if debit_text.strip() else None
        credit = parse_money(credit_text) if credit_text.strip() else None
        if debit is not None:
            return debit.magnitude_minor, "outflow"
        if credit is not None:
            return -credit.magnitude_minor, "inflow"
        return None

    amount_col = next((c for c in money_cols if (c.role or "amount") == "amount"), None)
    if amount_col is None:
        return None  # pragma: no cover - _build_doc guarantees an amount/debit/credit column
    text = band_values.get(amount_col.name, "")
    if not text.strip():
        return None
    parsed = parse_money(text)
    if parsed is None:
        return None

    flow: _Flow
    if sign.inflow == "leading_minus" and sign.outflow != "leading_minus":
        flow = "inflow" if parsed.printed_negative else "outflow"
    elif sign.outflow == "leading_minus" and sign.inflow != "leading_minus":
        flow = "outflow" if parsed.printed_negative else "inflow"
    else:
        flow = "outflow"

    amount_minor = -parsed.magnitude_minor if flow == "inflow" else parsed.magnitude_minor
    return amount_minor, flow


def _kind_hint_for_flow(pattern: SectionPattern | None, flow: _Flow) -> KindHint | None:
    """The `kind_hint` for a row given the section pattern (if any) it fell under and its
    resolved flow direction — a flow-specific hint wins over the pattern's plain `kind_hint`."""
    if pattern is None:
        return None
    if flow == "outflow":
        return pattern.outflow_kind_hint or pattern.kind_hint
    return pattern.inflow_kind_hint or pattern.kind_hint


class SpecParser:
    """A `StatementParser` (§3.1) driven entirely by a `LayoutSpecDoc` — no code, only data."""

    def __init__(
        self, doc: LayoutSpecDoc, *, parser_id: str | None = None, version: str | None = None
    ) -> None:
        self._doc = doc
        self._bands = tuple(Band(name=c.name, x0=c.x0, x1=c.x1) for c in doc.columns)
        self.id = parser_id or f"spec_{doc.id}_v{doc.version}"
        self.version = version or str(doc.version)
        self.account_type = doc.account_type

    def detect(self, doc: ExtractedDoc) -> float:
        """Confidence that this spec's `detect` block matches ``doc``. Never raises (§3.1)."""
        try:
            text = doc.full_text
            spec_detect = self._doc.detect
            if any(term not in text for term in spec_detect.all_of):
                return 0.0
            if spec_detect.any_of and not any(term in text for term in spec_detect.any_of):
                return 0.0
            return spec_detect.score
        except Exception:  # pragma: no cover - defensive; detect() must never raise
            return 0.0

    def parse(self, doc: ExtractedDoc) -> ParsedStatement:
        """Interpret ``doc`` against the spec. Raises `ParserError` when a year-less date cannot
        be resolved against the statement period (per §2c common requirements)."""
        spec = self._doc
        sections = spec.sections
        columns = spec.columns
        money_cols = [c for c in columns if c.type == "money"]
        desc_col = next((c for c in columns if c.name == "description"), None)
        date_col = next(c for c in columns if c.name == "posted_date")
        txn_date_col = next((c for c in columns if c.name == "transaction_date"), None)
        issuer_cat_col = next((c for c in columns if c.name == "issuer_category"), None)

        summary = extract_period_and_balances(doc)

        mask: str | None = None
        if spec.account_mask_pattern is not None:
            mask_match = spec.account_mask_pattern.search(doc.full_text)
            if mask_match is not None and mask_match.groups():
                raw_mask = mask_match.group(1)
                mask = raw_mask[-4:] if raw_mask else None

        rows_out: list[dict[str, Any]] = []
        current_pattern: SectionPattern | None = None
        current_section: str | None = None
        excluding = False
        terminated = False

        for page in doc.pages:
            if terminated:
                break
            for row in cluster_rows(page.words):
                text = row.text.strip()
                if not text:
                    continue
                if (
                    sections is not None
                    and sections.terminator is not None
                    and sections.terminator.search(text)
                ):
                    terminated = True
                    break

                heading_matched = False
                if sections is not None:
                    for excl in sections.exclude_tables:
                        if excl.search(text):
                            excluding = True
                            heading_matched = True
                            break
                    # §2c: disambiguate by which table we are inside, never by label alone.
                    # Once excluding, a section pattern can never clear it — an excluded table
                    # (e.g. INTEREST CHARGED) can itself contain a row bearing a section label
                    # (e.g. "PURCHASES 22.99% ...") that is not a real heading. Only a
                    # terminator or another excluded-table match changes state.
                    if not heading_matched and not excluding:
                        for pattern in sections.patterns:
                            if pattern.match.search(text):
                                current_pattern = pattern
                                current_section = pattern.match_text
                                heading_matched = True
                                break
                if heading_matched:
                    continue
                if excluding:
                    # A row inside an excluded table is never continuation text either.
                    continue

                band_values = assign_to_bands(row, self._bands)
                date_text = band_values.get("posted_date", "")
                partial = parse_date(date_text, date_col.formats) if date_text.strip() else None

                if partial is None:
                    # A dateless row matching `fx` (§2c) sets the previous transaction's foreign
                    # currency fields and never touches its description; only when it doesn't
                    # match do we fall through to the plain multiline-continuation merge.
                    if rows_out and spec.fx is not None and self._apply_fx_line(rows_out[-1], text):
                        continue
                    # A continuation line for the previous transaction's multiline description
                    # (no date, not a heading/excluded/terminator row): take the whole row, words
                    # ordered left to right, not just whatever fell in the description band —
                    # a continuation printed from the left margin can span the date band too.
                    if rows_out and desc_col is not None and desc_col.multiline and text:
                        last = rows_out[-1]
                        last["description"] = f"{last['description']} {text}".strip()
                    continue

                resolved = _resolve_amount_and_flow(band_values, money_cols, spec.sign)
                if resolved is None:
                    continue
                amount_minor, flow = resolved
                kind_hint = _kind_hint_for_flow(current_pattern, flow)

                posted_date = self._resolve_date(
                    partial, summary.period_start, summary.period_end, text
                )

                transaction_date: date | None = None
                if txn_date_col is not None:
                    td_text = band_values.get("transaction_date", "").strip()
                    if td_text:
                        td_partial = parse_date(td_text, txn_date_col.formats)
                        if td_partial is not None:
                            transaction_date = self._resolve_date(
                                td_partial, summary.period_start, summary.period_end, text
                            )

                issuer_category: str | None = None
                if issuer_cat_col is not None:
                    issuer_category = band_values.get("issuer_category", "").strip() or None

                rows_out.append(
                    {
                        "posted_date": posted_date,
                        "transaction_date": transaction_date,
                        "description": band_values.get("description", "").strip(),
                        "amount_minor": amount_minor,
                        "kind_hint": kind_hint,
                        "section": current_section,
                        "issuer_category": issuer_category,
                        "fx_amount_minor": None,
                        "fx_currency": None,
                        "fx_rate": None,
                    }
                )

        transactions = tuple(
            RawTransaction(
                posted_date=r["posted_date"],
                transaction_date=r["transaction_date"],
                description=r["description"],
                amount_minor=r["amount_minor"],
                currency=spec.currency,
                fx_amount_minor=r["fx_amount_minor"],
                fx_currency=r["fx_currency"],
                fx_rate=r["fx_rate"],
                kind_hint=r["kind_hint"],
                section=r["section"],
                issuer_category=r["issuer_category"],
            )
            for r in rows_out
        )

        section_totals: tuple[tuple[str, int], ...] = ()
        if spec.totals.section_totals:
            totals_by_label: dict[str, int] = {}
            order: list[str] = []
            for txn in transactions:
                label = txn.section or ""
                if label not in totals_by_label:
                    totals_by_label[label] = 0
                    order.append(label)
                totals_by_label[label] += txn.amount_minor
            section_totals = tuple((label, totals_by_label[label]) for label in order)

        return ParsedStatement(
            account_hint=AccountHint(
                account_type=spec.account_type, mask=mask, currency=spec.currency
            ),
            period_start=summary.period_start,
            period_end=summary.period_end,
            stated_total_minor=None,
            transactions=transactions,
            section_totals=section_totals,
            opening_balance_minor=summary.opening_balance_minor,
            closing_balance_minor=summary.closing_balance_minor,
        )

    def _resolve_date(
        self, partial: Any, period_start: date | None, period_end: date | None, row_text: str
    ) -> date:
        if partial.year is not None:
            return date(partial.year, partial.month, partial.day)
        if (
            self._doc.year_inference == "from_period"
            and period_start is not None
            and period_end is not None
        ):
            return infer_year(partial, period_start, period_end)
        raise ParserError(
            f"spec {self._doc.id!r}: cannot resolve a year for date on row {row_text!r} "
            "(no statement period was found and the date carries no explicit year)"
        )

    def _apply_fx_line(self, pending: dict[str, Any], text: str) -> bool:
        """If ``text`` (a dateless row) matches `fx`, set ``pending``'s foreign-currency fields
        in place and return `True` — the caller must not also treat ``text`` as continuation
        description text. Returns `False` when `fx` is unset or ``text`` doesn't match, or when a
        declared group is absent from the match (malformed spec data, not a crash)."""
        fx = self._doc.fx
        if fx is None:
            return False
        match = fx.pattern.search(text)
        if match is None:
            return False
        try:
            amount_text = match.group(fx.amount_group)
            currency_text = match.group(fx.currency_group)
            rate_text = match.group(fx.rate_group)
        except IndexError:
            return False

        fx_money = parse_money(amount_text) if amount_text else None
        pending["fx_amount_minor"] = fx_money.magnitude_minor if fx_money is not None else None
        pending["fx_currency"] = currency_text.strip().upper() if currency_text else None
        try:
            pending["fx_rate"] = float(rate_text) if rate_text else None
        except ValueError:
            pending["fx_rate"] = None
        return True


# ------------------------------------------------------------------------------------------------
# load_spec: the single entry point for every spec, regardless of origin (§2d.2, §2e.2)
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadedSpec:
    """The result of `load_spec`: a validated spec, ready to run, not yet persisted or approved."""

    name: str
    version: int
    parser_id: str
    spec_yaml: str
    source: _SourceLiteral
    doc: LayoutSpecDoc
    parser: SpecParser
    approved: bool = False


def load_spec(text: str, *, source: _SourceLiteral) -> LoadedSpec:
    """Validate, parse and compile a layout spec from YAML ``text``.

    The single entry point for every spec regardless of where it came from (§2d.2 "paste a layout
    spec", §2e.2 an LLM proposal, or a hand-mapped one from the layout mapper) — every origin runs
    identical validation and comes back with ``approved=False``; no origin is trusted more than
    another. An invalid spec is rejected with field-level errors, never partially applied.

    Raises:
        LayoutSpecError: the YAML does not parse, fails schema validation, or fails a semantic
            check (duplicate/missing columns, an unsafe regex, ...).
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LayoutSpecError(
            "layout spec is not valid YAML", (SpecFieldError("<root>", str(exc)),)
        ) from exc

    if not isinstance(data, dict):
        raise LayoutSpecError(
            "layout spec must be a YAML mapping",
            (SpecFieldError("<root>", f"got {type(data).__name__}"),),
        )

    schema_errors = validate_against_schema(data, _SCHEMA)
    if schema_errors:
        raise LayoutSpecError("layout spec failed schema validation", tuple(schema_errors))

    doc = _build_doc(data)
    parser = SpecParser(doc)
    return LoadedSpec(
        name=doc.id,
        version=doc.version,
        parser_id=parser.id,
        spec_yaml=text,
        source=source,
        doc=doc,
        parser=parser,
        approved=False,
    )


# ------------------------------------------------------------------------------------------------
# Persistence: immutable (name, version) rows (A21, §2f.1)
# ------------------------------------------------------------------------------------------------


def revise(
    session: Session,
    loaded: LoadedSpec,
    *,
    name: str | None = None,
    issuer_id: int | None = None,
    account_type: str | None = None,
) -> LayoutSpec:
    """Insert the next ``(name, version)`` row for ``name`` (default: ``loaded.name``).

    Never mutates an existing row: the new row's ``version`` is ``1 + max(existing version for
    name)`` (or ``1`` if none exist), independent of whatever ``version`` the spec's own YAML
    declares — the database row is the authoritative version, per A21. Flushes but does not
    commit; the caller controls the transaction.
    """
    row_name = name if name is not None else loaded.name
    existing_max = session.execute(
        select(func.max(LayoutSpec.version)).where(LayoutSpec.name == row_name)
    ).scalar_one_or_none()
    new_version = (existing_max or 0) + 1
    row = LayoutSpec(
        name=row_name,
        version=new_version,
        parser_id=f"spec_{loaded.doc.id}_v{new_version}",
        issuer_id=issuer_id,
        account_type=account_type if account_type is not None else loaded.doc.account_type,
        spec_yaml=loaded.spec_yaml,
        source=loaded.source,
        approved=False,
    )
    session.add(row)
    session.flush()
    return row


def approve_spec(session: Session, row: LayoutSpec) -> None:
    """Mark a persisted spec version approved, making it eligible for `resolve_spec`. Approving a
    version is a lifecycle flag, not an edit to its (immutable) content (A21)."""
    row.approved = True
    session.flush()


def resolve_spec(session: Session, *, issuer_id: int, account_type: str) -> LayoutSpec | None:
    """Return the highest-version, approved spec for ``(issuer_id, account_type)`` (§2f.1).

    A row whose ``account_type`` is ``NULL`` applies to any account type for that issuer.
    """
    stmt = (
        select(LayoutSpec)
        .where(
            LayoutSpec.issuer_id == issuer_id,
            LayoutSpec.approved.is_(True),
            (LayoutSpec.account_type == account_type) | (LayoutSpec.account_type.is_(None)),
        )
        .order_by(LayoutSpec.version.desc())
    )
    return session.execute(stmt).scalars().first()


def build_parser_for_row(row: LayoutSpec) -> SpecParser:
    """Rebuild a runnable `SpecParser` from a persisted `layout_specs` row, bound to that row's own
    ``parser_id`` and ``version`` — so `statements.layout_spec_id` and `parser_version` always
    agree (A21)."""
    data = yaml.safe_load(row.spec_yaml)
    doc = _build_doc(data)
    return SpecParser(doc, parser_id=row.parser_id, version=str(row.version))
