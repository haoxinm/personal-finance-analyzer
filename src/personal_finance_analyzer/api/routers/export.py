"""`GET /api/export` (§3.12). **Must exclude `accounts.mask`** — this is the data-leak regression
the plan explicitly calls out, so `tests/api/test_export.py` asserts it directly on the response
body rather than only on the schema.
"""

from __future__ import annotations

import csv
import io
from dataclasses import replace
from typing import Literal

from fastapi import APIRouter
from starlette.responses import PlainTextResponse

from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SessionDep
from personal_finance_analyzer.api.routers.transactions import ExportFiltersDep
from personal_finance_analyzer.api.services import crud

router = APIRouter(tags=["export"])

#: `schemas.Transaction` fields that are list-view extras (one-click suggestions), not exported.
_NOT_EXPORTED = {"candidates", "conflict"}

_FIELDS = (
    "id",
    "statement_id",
    "account_id",
    "posted_date",
    "transaction_date",
    "description_raw",
    "description_clean",
    "amount_minor",
    "currency",
    "kind",
    "category_key",
    "subcategory_key",
    "merchant_key",
    "notes",
    "needs_review",
)


#: A spreadsheet treats a cell starting with one of these as a formula (CSV injection): a
#: description like `=HYPERLINK(...)` copied from a statement would run when the export is opened.
#: `|` starts a DDE-style command in some spreadsheets. Leading whitespace is skipped first, since
#: spreadsheets ignore it (` =1+1` is still a formula).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "|")


def _csv_safe(value: object) -> object:
    """Neutralize a text cell that a spreadsheet would read as a formula by prefixing `'`, which
    the spreadsheet hides. Numbers are left alone (a negative `amount_minor` must stay numeric)."""
    if not isinstance(value, str) or not value:
        return value
    if value[0] in "\t\r\n" or value.lstrip().startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


@router.get("/export")
def export_transactions(
    session: SessionDep,
    filters: ExportFiltersDep,
    format: Literal["csv", "json"] = "csv",
    user_id: int | None = None,
) -> PlainTextResponse:
    """Export the transactions matching `filters` (the same filters as `GET /transactions`;
    unfiltered, everything), oldest first. `user_id` is shorthand for `user_ids=[user_id]`: the
    CLI's `export --user-id`/`--all-users` call this directly with a resolved id or `None`. The
    CLI passes `filters=crud.TransactionFilters(currency=None, include_non_spend=True)`."""
    if user_id is not None:
        filters = replace(filters, user_ids=[user_id])
    transactions = crud.export_transactions(session, filters)
    rows = [
        schemas.Transaction(
            id=t.id,
            statement_id=t.statement_id,
            account_id=t.account_id,
            posted_date=t.posted_date,
            transaction_date=t.transaction_date,
            description_raw=t.description_raw,
            description_clean=t.description_clean,
            amount_minor=t.amount_minor,
            currency=t.currency,
            kind=t.kind,
            category_key=crud.category_key_of(session, t),
            subcategory_key=crud.subcategory_key_of(session, t),
            merchant_key=t.merchant_key,
            notes=t.notes,
            needs_review=t.needs_review,
        )
        for t in transactions
    ]

    if format == "json":
        body = "[" + ",".join(row.model_dump_json(exclude=_NOT_EXPORTED) for row in rows) + "]"
        return PlainTextResponse(body, media_type="application/json")

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {k: _csv_safe(v) for k, v in row.model_dump(mode="json", exclude=_NOT_EXPORTED).items()}
        )
    return PlainTextResponse(buffer.getvalue(), media_type="text/csv")
