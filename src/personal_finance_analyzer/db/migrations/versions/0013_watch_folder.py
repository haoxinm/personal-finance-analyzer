"""watch_files (folder imports)

Revision ID: 0013_watch_folder
Revises: 0012_drop_unplaced_merchant_cache
Create Date: 2026-11-09 00:00:00

What the background folder scan did with each PDF it found in the documents folder (`watch/`), so
a file that failed is not retried until it changes. Like `0005`, the table is created from the
model's own `Table` object so the migrated schema is byte-identical to `create_all`, and it is
idempotent because `0001_initial` runs `create_all` from the *current* models.
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


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    Base.metadata.create_all(bind=bind, tables=[t for t in _TABLES if t.name not in existing])


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    Base.metadata.drop_all(bind=bind, tables=[t for t in reversed(_TABLES) if t.name in existing])
