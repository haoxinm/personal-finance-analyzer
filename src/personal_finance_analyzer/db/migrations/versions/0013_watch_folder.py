"""watch_files (folder imports) and the `pdf_in_place` flag of statements and tax documents

Revision ID: 0013_watch_folder
Revises: 0012_drop_unplaced_merchant_cache
Create Date: 2026-11-09 00:00:00

What the background folder scan did with each PDF it found in the documents folder (`watch/`), so
a file that failed is not retried until it changes. Like `0005`, the table is created from the
model's own `Table` object so the migrated schema is byte-identical to `create_all`, and it is
idempotent because `0001_initial` runs `create_all` from the *current* models.

`statements.pdf_in_place` / `tax_documents.pdf_in_place` mark a row whose PDF is a file the user
put in the documents folder themselves: the app never renames, moves or deletes such a file on its
own. Existing rows are all ``0``: before this revision every kept PDF was the app's own copy. A
plain `ADD COLUMN` with a server default, guarded like `0003`/`0006` because a fresh database
already has the columns (`0001_initial` builds the schema from the current models).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from personal_finance_analyzer.db.models import Base

# revision identifiers, used by Alembic.
revision: str = "0013_watch_folder"
down_revision: str | None = "0012_drop_unplaced_merchant_cache"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLES = [Base.metadata.tables["watch_files"]]
_FLAGGED = ("statements", "tax_documents")


def _has_flag(table: str) -> bool:
    return any(c["name"] == "pdf_in_place" for c in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    Base.metadata.create_all(bind=bind, tables=[t for t in _TABLES if t.name not in existing])
    for table in _FLAGGED:
        if not _has_flag(table):
            op.add_column(
                table,
                sa.Column(
                    "pdf_in_place", sa.Boolean(), nullable=False, server_default=sa.text("0")
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    Base.metadata.drop_all(bind=bind, tables=[t for t in reversed(_TABLES) if t.name in existing])
    for table in reversed(_FLAGGED):
        if _has_flag(table):
            # Native DROP COLUMN (SQLite >= 3.35), like 0006: a batch recreate of these tables
            # could cascade-delete their children through ON DELETE CASCADE foreign keys.
            op.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN pdf_in_place"))
