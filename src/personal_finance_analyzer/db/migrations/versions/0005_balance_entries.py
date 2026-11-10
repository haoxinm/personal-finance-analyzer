"""balance_entries (net worth)

Revision ID: 0005_balance_entries
Revises: 0004_tax_documents
Create Date: 2026-10-25 00:00:00

Unlike most revisions this one imports the models, deliberately: it creates the tables from the
models' own `Table` objects so the migrated schema is byte-identical to `create_all` (the schema
parity test). It is idempotent because `0001_initial` runs `create_all` from the *current* models,
so on a fresh database these tables already exist by the time this revision runs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from personal_finance_analyzer.db.models import Base

# revision identifiers, used by Alembic.
revision: str = "0005_balance_entries"
down_revision: str | None = "0004_tax_documents"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLES = [Base.metadata.tables["balance_entries"]]


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    Base.metadata.create_all(bind=bind, tables=[t for t in _TABLES if t.name not in existing])


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    Base.metadata.drop_all(bind=bind, tables=[t for t in reversed(_TABLES) if t.name in existing])
