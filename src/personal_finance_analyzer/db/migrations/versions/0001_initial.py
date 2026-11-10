"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-01 00:00:00

Creates every table and index of §3.2. Tables only — no data seeding (A3); `sync_taxonomy()`
seeds `categories`/`subcategories` idempotently at application startup, not here.

Table creation is delegated to the ORM metadata (`personal_finance_analyzer.db.models.Base.metadata`) so this
migration can never drift from the declarative models: a dedicated test
(`tests/db/test_schema_parity.py`) asserts the two paths produce identical `sqlite_master` output.
SQLAlchemy resolves the `issuers.default_spec_id` <-> `layout_specs.issuer_id` FK cycle (A29)
itself, because that foreign key is declared with ``use_alter=True`` on the model.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from personal_finance_analyzer.db.models import Base

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
