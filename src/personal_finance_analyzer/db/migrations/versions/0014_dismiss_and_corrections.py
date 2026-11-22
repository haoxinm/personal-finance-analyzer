"""`watch_files.dismissed_at` and the `orig_*` correction columns of `transactions`

Revision ID: 0014_dismiss_and_corrections
Revises: 0013_watch_folder
Create Date: 2026-11-22 00:00:00

`watch_files.dismissed_at` (nullable ISO timestamp): the user dismissed a failed/ignored folder-scan
problem. It stays hidden while the file is unchanged and is cleared whenever the scan rewrites the
record (the file changed, or Retry deleted the row).

`transactions.orig_posted_date` / `orig_amount_minor` / `orig_description_clean` (nullable): a
non-NULL value means the user corrected that field and this is the *imported* value; the live
column holds the corrected one. `dedupe_hash` is never recomputed.

Plain `ADD COLUMN`s, guarded because a fresh database already has the columns (`0001_initial`
builds the schema from the current models). Downgrade uses native `DROP COLUMN` (SQLite >= 3.35)
like `0006`/`0013`: a batch recreate could cascade-delete children through ON DELETE CASCADE.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_dismiss_and_corrections"
down_revision: str | None = "0013_watch_folder"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_COLUMNS: tuple[tuple[str, str, type[sa.types.TypeEngine[Any]]], ...] = (
    ("watch_files", "dismissed_at", sa.Text),
    ("transactions", "orig_posted_date", sa.Text),
    ("transactions", "orig_amount_minor", sa.Integer),
    ("transactions", "orig_description_clean", sa.Text),
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
