"""tax_profiles (manual inputs for the per-user federal tax estimate)

Revision ID: 0009_tax_profiles
Revises: 0008_income_memo_categories
Create Date: 2026-10-30 00:00:00

Like 0005 this imports the models deliberately: it creates the table from the model's own `Table`
object so the migrated schema is byte-identical to `create_all` (the schema parity test), and is
idempotent because `0001_initial` runs `create_all` from the *current* models, so on a fresh
database the table already exists by the time this revision runs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from personal_finance_analyzer.db.models import Base

# revision identifiers, used by Alembic.
revision: str = "0009_tax_profiles"
down_revision: str | None = "0008_income_memo_categories"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLES = [Base.metadata.tables["tax_profiles"]]


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    Base.metadata.create_all(bind=bind, tables=[t for t in _TABLES if t.name not in existing])


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    Base.metadata.drop_all(bind=bind, tables=[t for t in reversed(_TABLES) if t.name in existing])
