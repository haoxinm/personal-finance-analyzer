"""tax provenance: extraction_method / is_user_edited / origin columns

Revision ID: 0006_tax_provenance
Revises: 0005_balance_entries
Create Date: 2026-10-25 00:00:00

`tax_forms.extraction_method` (parser|llm|manual), `tax_forms.is_user_edited`, and
`income_items.origin` / `tax_items.origin` (parser|llm|manual). Plain `ADD COLUMN` with a server
default, so existing rows keep their data and the migrated schema equals `create_all`. No CHECK is
added: that would need a batch recreate of `tax_forms`, which is referenced by `ON DELETE CASCADE`
foreign keys. Values are validated in `ingest/tax/edit.py` and the API schemas.

Idempotent like 0003: `0001_initial` runs `create_all` from the current models, so a fresh
database already has these columns by the time this revision runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_tax_provenance"
down_revision: str | None = "0005_balance_entries"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_COLUMNS: tuple[tuple[str, str, sa.Column[Any]], ...] = (
    (
        "tax_forms",
        "extraction_method",
        sa.Column(
            "extraction_method", sa.String(), nullable=False, server_default=sa.text("'parser'")
        ),
    ),
    (
        "tax_forms",
        "is_user_edited",
        sa.Column("is_user_edited", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    ),
    (
        "income_items",
        "origin",
        sa.Column("origin", sa.String(), nullable=False, server_default=sa.text("'parser'")),
    ),
    (
        "tax_items",
        "origin",
        sa.Column("origin", sa.String(), nullable=False, server_default=sa.text("'parser'")),
    ),
)


def _has_column(table: str, column: str) -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(table)
    return any(c["name"] == column for c in columns)


def upgrade() -> None:
    for table, name, column in _COLUMNS:
        if not _has_column(table, name):
            op.add_column(table, column)


def downgrade() -> None:
    for table, name, _ in reversed(_COLUMNS):
        if _has_column(table, name):
            # Native DROP COLUMN (SQLite >= 3.35): a batch recreate of `tax_forms` could cascade-delete
            # item rows through the `ON DELETE CASCADE` foreign keys.
            op.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {name}"))
