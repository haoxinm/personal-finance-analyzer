"""`statements.parsed_row_count`, `issuers.default_parser_id` and `jobs.error_code`

Revision ID: 0015_import_attention
Revises: 0014_dismiss_and_corrections
Create Date: 2026-11-22 12:00:00

`statements.parsed_row_count` (nullable): rows the extractor returned, before dedupe. `txn_count`
only counts the rows inserted, so "0 rows because everything was already on file" and "the
extractor found nothing" could not be told apart. NULL for statements imported earlier.

`issuers.default_parser_id` (nullable): the built-in parser id (or ``llm``) the user asked to
remember for the issuer, the counterpart of `default_spec_id`.

`jobs.error_code` (nullable): a machine-readable failure code the UI can act on.

Plain `ADD COLUMN`s, guarded because a fresh database already has the columns (`0001_initial`
builds the schema from the current models). Downgrade uses native `DROP COLUMN` (SQLite >= 3.35)
like `0006`/`0013`/`0014`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_import_attention"
down_revision: str | None = "0014_dismiss_and_corrections"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_COLUMNS: tuple[tuple[str, str, type[sa.types.TypeEngine[Any]]], ...] = (
    ("statements", "parsed_row_count", sa.Integer),
    ("issuers", "default_parser_id", sa.Text),
    ("jobs", "error_code", sa.Text),
)


def _has(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    for table, column, type_ in _COLUMNS:
        if not _has(table, column):
            op.add_column(table, sa.Column(column, type_(), nullable=True))


def downgrade() -> None:
    for table, column, _type in reversed(_COLUMNS):
        if _has(table, column):
            op.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))
