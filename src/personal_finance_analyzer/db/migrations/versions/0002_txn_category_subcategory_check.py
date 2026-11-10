"""transactions: category/subcategory both-or-neither check constraint

Revision ID: 0002_txn_category_subcategory_check
Revises: 0001_initial
Create Date: 2026-09-28 00:00:00

Adds `ck_txn_category_subcategory_both_or_neither` — `(category_id IS NULL) = (subcategory_id IS
NULL)` — to `transactions` (I4). SQLite has no `ALTER TABLE ... ADD CONSTRAINT`, so this uses
Alembic's `batch_alter_table`, which recreates the table (copy-data-drop-rename) with the new
constraint applied; matches the model in `db/models.py`, which already declares this constraint,
so a fresh database (created via `Base.metadata.create_all`, e.g. in tests) and a database
migrated from `0001_initial` end up with the identical schema (see
`tests/db/test_schema_parity.py`).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_txn_category_subcategory_check"
down_revision: str | None = "0001_initial"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_CONSTRAINT_NAME = "ck_txn_category_subcategory_both_or_neither"
_CONDITION = "(category_id IS NULL) = (subcategory_id IS NULL)"


def upgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.create_check_constraint(_CONSTRAINT_NAME, _CONDITION)


def downgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
