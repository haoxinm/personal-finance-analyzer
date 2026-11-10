"""One typed builder feeding every spending view (§3.9, P1-E).

`SpendQuery` describes a request; `build_select()` turns it into a single SQLAlchemy Core
`select()`; `run_query()` executes it and returns tidy rows. **No ad-hoc SQL is written anywhere
else in this codebase** — routers and the CLI report must call into this module rather than
building their own `select()`/raw SQL, which is the entire point of this work package.

Every user-supplied *value* (dates, amounts, the `search` string, id lists, `currency`) becomes a
bound parameter — never string-interpolated into SQL. `group_by` and `granularity` are the only
values that reach SQL as identifiers (column names / `strftime` format strings), so they are
validated against the `Literal` allow-lists of `SpendQuery` before anything is built; a value
outside those allow-lists raises `ValueError` and never touches a query.

**Default kinds (A7):** when `kinds` is `None`, the default depends on `include_non_spend`:

- `include_non_spend=True`  → no kind filter at all (every kind passes through).
- `include_non_spend=False` → `{purchase, fee, interest}`, plus `refund` when `net_refunds=True`.
  Refunds carry `is_spend=0` and a *negative* `amount_minor` (I5); including them under
  `net_refunds` is what makes netting non-trivial — without this default, netting would be a
  no-op because refunds would already be excluded by default.

An explicit `kinds` list bypasses this default entirely and is used as given (still validated
against the `Kind` allow-list).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from sqlalchemy import ColumnElement, Integer, Select, String, case, cast, func, literal, select
from sqlalchemy.orm import Session, aliased

from personal_finance_analyzer.db.models import Account, Category, Subcategory, Transaction

Granularity = Literal["day", "week", "month", "quarter", "year", "all"]
GroupKey = Literal["category", "subcategory", "user", "account", "merchant", "period"]

_VALID_GRANULARITIES: frozenset[str] = frozenset({"day", "week", "month", "quarter", "year", "all"})
_VALID_GROUP_KEYS: frozenset[str] = frozenset(
    {"category", "subcategory", "user", "account", "merchant", "period"}
)
_VALID_KINDS: frozenset[str] = frozenset(
    {"purchase", "refund", "payment", "transfer", "fee", "interest", "adjustment"}
)

#: A7: the kinds counted as spend by default, before `net_refunds` adds `refund`.
_DEFAULT_SPEND_KINDS: frozenset[str] = frozenset({"purchase", "fee", "interest"})

# A24-style self-join aliases for subcategory-merge resolution (module-level: one pair of aliases
# reused by every `SpendQuery`, so `build_select` never constructs mismatched joins).
_SubOriginal = aliased(Subcategory, name="sub_original")
_SubTarget = aliased(Subcategory, name="sub_target")


@dataclass(frozen=True, slots=True)
class SpendQuery:
    """A request for aggregated spending, as described by §3.9. Every field is optional except
    the granularity/group-by/net-refunds/currency defaults, which reproduce the common "spending
    by category over time" view with no arguments."""

    user_ids: list[int] | None = None
    account_ids: list[int] | None = None
    date_from: date | None = None
    date_to: date | None = None
    granularity: Granularity = "month"
    group_by: list[GroupKey] = field(default_factory=lambda: ["period", "category"])
    category_keys: list[str] | None = None
    subcategory_keys: list[str] | None = None
    amount_min_minor: int | None = None
    amount_max_minor: int | None = None
    kinds: list[str] | None = None  # default per A7
    include_non_spend: bool = False
    net_refunds: bool = True
    search: str | None = None  # substring on description_clean
    currency: str = "USD"  # D5: filter, never convert


def _validate(query: SpendQuery) -> None:
    """Validate every value that will reach SQL as an identifier rather than a bound parameter.

    Raises:
        ValueError: `granularity`, an entry of `group_by`, or an entry of `kinds` is not one of
            the allowed literals, or `group_by` is empty.
    """
    if query.granularity not in _VALID_GRANULARITIES:
        raise ValueError(f"invalid granularity: {query.granularity!r}")
    if not query.group_by:
        raise ValueError("group_by must not be empty")
    for key in query.group_by:
        if key not in _VALID_GROUP_KEYS:
            raise ValueError(f"invalid group_by entry: {key!r}")
    if query.kinds is not None:
        for kind in query.kinds:
            if kind not in _VALID_KINDS:
                raise ValueError(f"invalid kind: {kind!r}")


def _period_expr(granularity: Granularity) -> ColumnElement[str]:
    """Return the SQL expression that buckets `posted_date` per `granularity` (§3.9).

    Day/month/year use SQLite `strftime`; week is the ISO 8601 week (`YYYY-Www`, the year being the
    ISO week-year, so 2025-12-29 is in `2026-W01` rather than a split `2025-W52`/`2026-W00` pair;
    SQLite has no `%G`/`%V`, so it is derived from the week's Thursday); quarter is
    `strftime('%Y', posted_date) || '-Q' || ((CAST(strftime('%m', posted_date) AS INTEGER) - 1) /
    3 + 1)` per §3.9; `"all"` collapses every row into a single literal bucket, `"all"`.
    """
    column = Transaction.posted_date
    if granularity == "day":
        return func.strftime("%Y-%m-%d", column)
    if granularity == "week":
        # The ISO week-year and week number both come from the week's Thursday: the next
        # Thursday on or after (date - 3 days) is that Thursday for every weekday.
        thursday = func.date(column, "-3 days", "weekday 4")
        iso_week = (cast(func.strftime("%j", thursday), Integer) - 1).self_group().op("/")(7) + 1
        return func.printf("%s-W%02d", func.strftime("%Y", thursday), iso_week)
    if granularity == "month":
        return func.strftime("%Y-%m", column)
    if granularity == "year":
        return func.strftime("%Y", column)
    if granularity == "quarter":
        year = func.strftime("%Y", column)
        month_int = cast(func.strftime("%m", column), Integer)
        # `.op("/")` rather than Python `/`: SQLAlchemy's default division operator on Integer
        # columns coerces to floating-point (adds `+ 0.0`) to match Python `truediv` semantics;
        # §3.9's formula is SQLite's truncating integer division.
        quarter = (month_int - 1).self_group().op("/")(3) + 1
        return year + "-Q" + cast(quarter, String)
    return literal("all")


def _resolved_subcategory_key() -> ColumnElement[str | None]:
    """The subcategory key to group by: a `status='merged'` subcategory resolves to its
    `merged_into` target's key (one level — merge chains are not expected to nest), so historical
    rows land in the merged bucket rather than a stale one."""
    return case(
        (_SubOriginal.status == "merged", _SubTarget.key),
        else_=_SubOriginal.key,
    )


def build_select(query: SpendQuery) -> Select[Any]:
    """Build the single SQLAlchemy Core `select()` that answers `query`.

    Returns rows shaped `{period?, category?, subcategory?, user?, account?, merchant?, currency,
    total_minor, txn_count, avg_minor}` — one column per requested `group_by` key (in the order
    given), plus the four aggregate columns, always present and always last. `total_minor` and
    `avg_minor` are signed minor units (I5); `avg_minor` is `total_minor / txn_count` (SQLite
    integer division on the aggregates, not a per-row average).

    Raises:
        ValueError: see `_validate`.
    """
    _validate(query)

    needs_category_join = "category" in query.group_by or query.category_keys is not None
    needs_subcategory_join = "subcategory" in query.group_by or query.subcategory_keys is not None
    needs_account_join = "account" in query.group_by or query.account_ids is not None

    resolved_subcategory_key = _resolved_subcategory_key() if needs_subcategory_join else None

    columns: list[Any] = []
    group_cols: list[Any] = []

    for key in query.group_by:
        if key == "period":
            col = _period_expr(query.granularity)
            columns.append(col.label("period"))
            group_cols.append(col)
        elif key == "category":
            columns.append(Category.key.label("category"))
            group_cols.append(Category.key)
        elif key == "subcategory":
            assert resolved_subcategory_key is not None
            columns.append(resolved_subcategory_key.label("subcategory"))
            group_cols.append(resolved_subcategory_key)
        elif key == "user":
            columns.append(Transaction.user_id.label("user"))
            group_cols.append(Transaction.user_id)
        elif key == "account":
            columns.append(Transaction.account_id.label("account"))
            group_cols.append(Transaction.account_id)
        elif key == "merchant":
            columns.append(Transaction.merchant_key.label("merchant"))
            group_cols.append(Transaction.merchant_key)

    columns.extend(
        [
            Transaction.currency.label("currency"),
            func.sum(Transaction.amount_minor).label("total_minor"),
            func.count(Transaction.id).label("txn_count"),
            # `.op("/")`: see the note in `_period_expr` on Integer division.
            func.sum(Transaction.amount_minor)
            .op("/")(func.count(Transaction.id))
            .label("avg_minor"),
        ]
    )
    group_cols.append(Transaction.currency)

    stmt: Select[Any] = select(*columns).select_from(Transaction)

    if needs_category_join:
        stmt = stmt.outerjoin(Category, Transaction.category_id == Category.id)
    if needs_subcategory_join:
        stmt = stmt.outerjoin(_SubOriginal, Transaction.subcategory_id == _SubOriginal.id)
        stmt = stmt.outerjoin(_SubTarget, _SubOriginal.merged_into == _SubTarget.id)
    if needs_account_join:
        stmt = stmt.outerjoin(Account, Transaction.account_id == Account.id)

    stmt = stmt.where(Transaction.currency == query.currency)

    if query.user_ids is not None:
        stmt = stmt.where(Transaction.user_id.in_(query.user_ids))
    if query.account_ids is not None:
        stmt = stmt.where(Transaction.account_id.in_(query.account_ids))
    if query.date_from is not None:
        stmt = stmt.where(Transaction.posted_date >= query.date_from.isoformat())
    if query.date_to is not None:
        stmt = stmt.where(Transaction.posted_date <= query.date_to.isoformat())
    if query.category_keys is not None:
        stmt = stmt.where(Category.key.in_(query.category_keys))
    if query.subcategory_keys is not None:
        assert resolved_subcategory_key is not None
        stmt = stmt.where(resolved_subcategory_key.in_(query.subcategory_keys))
    if query.amount_min_minor is not None:
        stmt = stmt.where(Transaction.amount_minor >= query.amount_min_minor)
    if query.amount_max_minor is not None:
        stmt = stmt.where(Transaction.amount_minor <= query.amount_max_minor)
    if query.search is not None:
        # `.contains(..., autoescape=True)` binds `search` as a parameter and escapes '%', '_'
        # and the escape character itself, so a search string containing '%', '_' or "'" matches
        # literally rather than as LIKE wildcards or breaking the query.
        stmt = stmt.where(Transaction.description_clean.contains(query.search, autoescape=True))

    kinds_filter = _resolve_kinds_filter(query)
    if kinds_filter is not None:
        stmt = stmt.where(Transaction.kind.in_(kinds_filter))

    stmt = stmt.group_by(*group_cols)
    return stmt


def _resolve_kinds_filter(query: SpendQuery) -> frozenset[str] | None:
    """Resolve the effective kind filter per A7. Returns `None` for "no filter"."""
    if query.kinds is not None:
        return frozenset(query.kinds)
    if query.include_non_spend:
        return None
    if query.net_refunds:
        return _DEFAULT_SPEND_KINDS | {"refund"}
    return _DEFAULT_SPEND_KINDS


def run_query(session: Session, query: SpendQuery) -> list[dict[str, object]]:
    """Execute `query` and return tidy rows: one dict per group, with keys `period`/`category`/
    `subcategory`/`user`/`account`/`merchant` (only those requested via `group_by`, in the order
    given) plus `currency`, `total_minor`, `txn_count`, and `avg_minor` (always present, always
    last). Directly chartable and directly CSV-exportable.
    """
    stmt = build_select(query)
    result = session.execute(stmt)
    return [dict(row._mapping) for row in result]


__all__ = ["SpendQuery", "build_select", "run_query"]
