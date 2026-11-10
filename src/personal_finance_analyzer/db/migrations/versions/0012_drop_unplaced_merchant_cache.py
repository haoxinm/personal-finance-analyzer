"""drop merchant-cache rows that only say "uncategorized"

Revision ID: 0012_drop_unplaced_merchant_cache
Revises: 0011_relative_stored_paths
Create Date: 2026-11-09 00:00:00

Before rows nothing could classify were recorded as `classified_by='unplaced'`, they were written to
the merchant cache (`merchant_map`) with `source='llm'` and category `others/uncategorized`. Later
imports then got a "cache" hit that answered "uncategorized", and the candidates endpoint offered a
bogus "learned from LLM" suggestion.

This deletes every `merchant_map` row with `source='llm'` and category `others/uncategorized`. Some
of them may have been genuine model answers; that is fine, because an uncategorized cache hit has
no value, and the only effect of dropping it is that the model is asked again next time.

Rows from any other source (`user`, `issuer`, `builtin`) and every rule are left alone. Nothing
else references `merchant_map` rows (no foreign keys point at it; transactions only carry the
merchant key as text), so there is nothing else to clean up.

Downgrade is a no-op: the deleted rows were placeholders, and there is nothing to restore.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_drop_unplaced_merchant_cache"
down_revision: str | None = "0011_relative_stored_paths"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "DELETE FROM merchant_map WHERE source = 'llm' AND subcategory_id IN ("
            "SELECT s.id FROM subcategories s JOIN categories c ON c.id = s.category_id "
            "WHERE c.key = 'others' AND s.key = 'uncategorized')"
        )
    )


def downgrade() -> None:
    """No-op: the deleted rows only said "uncategorized" and are not worth restoring."""
