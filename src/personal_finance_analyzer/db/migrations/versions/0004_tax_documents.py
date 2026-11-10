"""tax_documents, tax_forms, income_items, tax_items (income and tax analysis)

Revision ID: 0004_tax_documents
Revises: 0003_statement_selected_pages
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
revision: str = "0004_tax_documents"
down_revision: str | None = "0003_statement_selected_pages"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Parent tables first (FK order).
_TABLES = [
    Base.metadata.tables[name]
    for name in ("tax_documents", "tax_forms", "income_items", "tax_items")
]


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    Base.metadata.create_all(bind=bind, tables=[t for t in _TABLES if t.name not in existing])


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    Base.metadata.drop_all(bind=bind, tables=[t for t in reversed(_TABLES) if t.name in existing])
