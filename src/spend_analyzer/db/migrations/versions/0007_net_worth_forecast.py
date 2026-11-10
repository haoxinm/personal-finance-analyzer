"""net_worth_history and forecast_presets (net worth forecast)

Revision ID: 0007_net_worth_forecast
Revises: 0006_tax_provenance
Create Date: 2026-10-30 00:00:00

Like 0005 this creates the tables from the models' own `Table` objects so the migrated schema is
byte-identical to `create_all` (the schema parity test), and is idempotent because `0001_initial`
runs `create_all` from the *current* models, so on a fresh database these tables already exist by
the time this revision runs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from spend_analyzer.db.models import Base

# revision identifiers, used by Alembic.
revision: str = "0007_net_worth_forecast"
down_revision: str | None = "0006_tax_provenance"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLES = [
    Base.metadata.tables["net_worth_history"],
    Base.metadata.tables["forecast_presets"],
]


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    Base.metadata.create_all(bind=bind, tables=[t for t in _TABLES if t.name not in existing])


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    Base.metadata.drop_all(bind=bind, tables=[t for t in reversed(_TABLES) if t.name in existing])
