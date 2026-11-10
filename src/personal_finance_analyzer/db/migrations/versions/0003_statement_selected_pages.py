"""statements: selected_pages (JSON list of pages to parse; NULL = all)

Revision ID: 0003_statement_selected_pages
Revises: 0002_txn_category_subcategory_check
Create Date: 2026-10-25 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_statement_selected_pages"
down_revision: str | None = "0002_txn_category_subcategory_check"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _has_column() -> bool:
    # 0001_initial builds the schema from the current models (`create_all`), so a fresh database
    # may already have the column by the time this revision runs.
    columns = sa.inspect(op.get_bind()).get_columns("statements")
    return any(c["name"] == "selected_pages" for c in columns)


def upgrade() -> None:
    if not _has_column():
        op.add_column("statements", sa.Column("selected_pages", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column():
        with op.batch_alter_table("statements") as batch_op:
            batch_op.drop_column("selected_pages")
