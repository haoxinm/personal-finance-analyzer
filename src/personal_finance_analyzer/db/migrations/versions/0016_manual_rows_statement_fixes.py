"""Manual rows, excluded rows and statement-level fixes

Revision ID: 0016_manual_rows_statement_fixes
Revises: 0015_import_attention
Create Date: 2026-11-23 12:00:00

`transactions.source` ('parsed' | 'manual') and `transactions.excluded`; `statements.flip_sign`,
`statements.period_corrected`, `statements.orig_period_start` / `orig_period_end`.

`statements.parsed_total_minor` goes back to meaning "sum of the parsed rows as extracted": the
reconciliation now adds a live adjustment (corrections, manual rows, exclusions) instead of
mutating the total on every edit. Existing totals carried the corrections' deltas, so they are
normalized here (downgrade adds them back).

Plain `ADD COLUMN`s, guarded because a fresh database already has the columns (`0001_initial`
builds the schema from the current models). Downgrade uses native `DROP COLUMN` (SQLite >= 3.35).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_manual_rows_statement_fixes"
down_revision: str | None = "0015_import_attention"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _columns() -> tuple[tuple[str, sa.Column[Any]], ...]:
    return (
        ("transactions", sa.Column("source", sa.String(), nullable=False, server_default="parsed")),
        ("transactions", sa.Column("excluded", sa.Boolean(), nullable=False, server_default="0")),
        ("statements", sa.Column("flip_sign", sa.Boolean(), nullable=False, server_default="0")),
        (
            "statements",
            sa.Column("period_corrected", sa.Boolean(), nullable=False, server_default="0"),
        ),
        ("statements", sa.Column("orig_period_start", sa.Text(), nullable=True)),
        ("statements", sa.Column("orig_period_end", sa.Text(), nullable=True)),
    )


_DELTA_SQL = (
    "(SELECT COALESCE(SUM(t.amount_minor - t.orig_amount_minor), 0) FROM transactions t "
    "WHERE t.statement_id = statements.id AND t.orig_amount_minor IS NOT NULL)"
)


def _has(table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    for table, column in _columns():
        if not _has(table, column.name):
            op.add_column(table, column)
    # Pre-0016 totals included the corrections' deltas; the live adjustment adds them now.
    op.execute(
        sa.text(
            f"UPDATE statements SET parsed_total_minor = parsed_total_minor - {_DELTA_SQL} "
            "WHERE parsed_total_minor IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM transactions t WHERE t.statement_id = statements.id "
            "AND t.orig_amount_minor IS NOT NULL)"
        )
    )


def downgrade() -> None:
    """Drops the added columns. Hand-added (manual) rows are kept: without ``source`` they
    become ordinary rows (their ``manual:`` hash prefix remains), and a re-import will no longer
    absorb them."""
    op.execute(
        sa.text(
            f"UPDATE statements SET parsed_total_minor = parsed_total_minor + {_DELTA_SQL} "
            "WHERE parsed_total_minor IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM transactions t WHERE t.statement_id = statements.id "
            "AND t.orig_amount_minor IS NOT NULL)"
        )
    )
    for table, column in reversed(_columns()):
        if not _has(table, column.name):
            continue
        try:
            op.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column.name}"))
        except sa.exc.OperationalError:
            # A database created by 0001 from the current models carries `ck_txn_source`, and
            # SQLite refuses to drop a column a CHECK refers to. The column is harmless to keep.
            if column.name != "source":
                raise
