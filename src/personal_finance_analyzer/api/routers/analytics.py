"""`/api/analytics/*` (§3.12, §3.9). Every route builds a `SpendQuery` and calls
`analytics/query.py` — no ad-hoc SQL here, per that module's own docstring."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from personal_finance_analyzer.analytics.query import Granularity, GroupKey, SpendQuery, run_query
from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SessionDep
from personal_finance_analyzer.api.services import crud

router = APIRouter(tags=["analytics"])


def _build_query(
    *,
    user_ids: list[int] | None,
    account_ids: list[int] | None,
    date_from: date | None,
    date_to: date | None,
    granularity: Granularity,
    group_by: list[GroupKey] | None,
    category_keys: list[str] | None,
    subcategory_keys: list[str] | None,
    amount_min_minor: int | None,
    amount_max_minor: int | None,
    kinds: list[str] | None,
    include_non_spend: bool,
    net_refunds: bool,
    search: str | None,
    currency: str,
) -> SpendQuery:
    return SpendQuery(
        user_ids=user_ids,
        account_ids=account_ids,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity,
        group_by=group_by if group_by else ["period", "category"],
        category_keys=category_keys,
        subcategory_keys=subcategory_keys,
        amount_min_minor=amount_min_minor,
        amount_max_minor=amount_max_minor,
        kinds=kinds,
        include_non_spend=include_non_spend,
        net_refunds=net_refunds,
        search=search,
        currency=currency,
    )


def _rows_to_schema(rows: list[dict[str, object]]) -> list[schemas.AnalyticsRow]:
    return [
        schemas.AnalyticsRow(
            period=row.get("period"),
            category=row.get("category"),
            subcategory=row.get("subcategory"),
            user=row.get("user"),
            account=row.get("account"),
            merchant=row.get("merchant"),
            currency=row["currency"],
            total_minor=row["total_minor"],
            txn_count=row["txn_count"],
            avg_minor=row["avg_minor"],
        )
        for row in rows
    ]


@router.get("/analytics/summary", response_model=list[schemas.AnalyticsRow])
def analytics_summary(
    session: SessionDep,
    user_ids: Annotated[list[int] | None, Query()] = None,
    account_ids: Annotated[list[int] | None, Query()] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    granularity: Granularity = "month",
    group_by: Annotated[list[GroupKey] | None, Query()] = None,
    category_keys: Annotated[list[str] | None, Query()] = None,
    subcategory_keys: Annotated[list[str] | None, Query()] = None,
    amount_min_minor: int | None = None,
    amount_max_minor: int | None = None,
    kinds: Annotated[list[schemas.Kind] | None, Query()] = None,
    include_non_spend: bool = False,
    net_refunds: bool = True,
    search: str | None = None,
    currency: str = "USD",
) -> list[schemas.AnalyticsRow]:
    try:
        query = _build_query(
            user_ids=user_ids,
            account_ids=account_ids,
            date_from=date_from,
            date_to=date_to,
            granularity=granularity,
            group_by=list(group_by) if group_by else None,
            category_keys=category_keys,
            subcategory_keys=subcategory_keys,
            amount_min_minor=amount_min_minor,
            amount_max_minor=amount_max_minor,
            kinds=list(kinds) if kinds else None,
            include_non_spend=include_non_spend,
            net_refunds=net_refunds,
            search=search,
            currency=currency,
        )
        rows = run_query(session, query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _rows_to_schema(rows)


@router.get("/analytics/timeseries", response_model=list[schemas.AnalyticsRow])
def analytics_timeseries(
    session: SessionDep,
    user_ids: Annotated[list[int] | None, Query()] = None,
    account_ids: Annotated[list[int] | None, Query()] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    granularity: Granularity = "month",
    group_by: Annotated[list[GroupKey] | None, Query()] = None,
    category_keys: Annotated[list[str] | None, Query()] = None,
    subcategory_keys: Annotated[list[str] | None, Query()] = None,
    kinds: Annotated[list[schemas.Kind] | None, Query()] = None,
    include_non_spend: bool = False,
    net_refunds: bool = True,
    currency: str = "USD",
) -> list[schemas.AnalyticsRow]:
    try:
        query = _build_query(
            user_ids=user_ids,
            account_ids=account_ids,
            date_from=date_from,
            date_to=date_to,
            granularity=granularity,
            group_by=list(group_by) if group_by else ["period"],
            category_keys=category_keys,
            subcategory_keys=subcategory_keys,
            amount_min_minor=None,
            amount_max_minor=None,
            kinds=list(kinds) if kinds else None,
            include_non_spend=include_non_spend,
            net_refunds=net_refunds,
            search=None,
            currency=currency,
        )
        rows = run_query(session, query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _rows_to_schema(rows)


@router.get("/analytics/top-merchants", response_model=list[schemas.TopMerchantRow])
def analytics_top_merchants(
    session: SessionDep,
    user_ids: Annotated[list[int] | None, Query()] = None,
    account_ids: Annotated[list[int] | None, Query()] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    category_keys: Annotated[list[str] | None, Query()] = None,
    currency: str = "USD",
    limit: int = 20,
) -> list[schemas.TopMerchantRow]:
    try:
        query = SpendQuery(
            user_ids=user_ids,
            account_ids=account_ids,
            date_from=date_from,
            date_to=date_to,
            granularity="all",
            group_by=["merchant"],
            category_keys=category_keys,
            currency=currency,
        )
        rows = run_query(session, query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    # Rows with no merchant key (nothing to rank or display) are dropped *before* the limit, so a
    # large un-keyed bucket never crowds a real merchant out of the top N.
    ranked = sorted(
        (row for row in rows if row["merchant"]),
        key=lambda r: int(r["total_minor"]),  # type: ignore[call-overload]
        reverse=True,
    )
    top_rows = ranked[: max(min(limit, 100), 1)]
    display_names = crud.merchant_display_names(session, [str(row["merchant"]) for row in top_rows])
    return [
        schemas.TopMerchantRow(
            merchant=row["merchant"],
            display_name=display_names.get(str(row["merchant"]), str(row["merchant"])),
            total_minor=row["total_minor"],
            txn_count=row["txn_count"],
        )
        for row in top_rows
    ]
