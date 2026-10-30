"""income_items: allow the W-2 box 12 memo categories

Revision ID: 0008_income_memo_categories
Revises: 0007_net_worth_forecast
Create Date: 2026-10-30 00:00:00

Adds `retirement_deferral` and `hsa_employer` to `ck_income_category`. SQLite cannot alter a CHECK
constraint, so this uses `batch_alter_table`, which recreates `income_items` (a leaf table: nothing
references it, so no `ON DELETE CASCADE` can fire) with the model's constraint. Idempotent like
0003: `0001_initial` runs `create_all` from the *current* models, so a fresh database already has
the new constraint by the time this revision runs. Downgrading deletes the rows that use the new
categories (they could not be stored under the old constraint).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_income_memo_categories"
down_revision: str | None = "0007_net_worth_forecast"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_NAME = "ck_income_category"
_OLD = (
    "salary_wages",
    "interest",
    "ordinary_dividends",
    "qualified_dividends",
    "short_term_capital_gain",
    "long_term_capital_gain",
    "self_employment",
    "other",
)
_NEW = (*_OLD, "retirement_deferral", "hsa_employer")


def _condition(values: Sequence[str]) -> str:
    return "category IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _has_new_categories() -> bool:
    sql = (
        op.get_bind()
        .execute(
            sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'income_items'")
        )
        .scalar_one_or_none()
    )
    return sql is not None and "retirement_deferral" in sql


def _recreate(values: Sequence[str]) -> None:
    with op.batch_alter_table("income_items", recreate="always") as batch_op:
        batch_op.drop_constraint(_NAME, type_="check")
        batch_op.create_check_constraint(_NAME, _condition(values))


def upgrade() -> None:
    if not _has_new_categories():
        _recreate(_NEW)


def downgrade() -> None:
    if _has_new_categories():
        op.execute(
            sa.text(
                "DELETE FROM income_items WHERE category IN ('retirement_deferral', 'hsa_employer')"
            )
        )
        _recreate(_OLD)
