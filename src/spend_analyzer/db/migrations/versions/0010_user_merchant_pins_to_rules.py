"""convert legacy user merchant pins into visible exact rules

Revision ID: 0010_user_merchant_pins_to_rules
Revises: 0009_tax_profiles
Create Date: 2026-10-30 00:00:00

Older versions wrote a `merchant_map` row with `source='user'` on every single-transaction edit, a
silent merchant-level pin that decided every future transaction of that merchant and that nothing
in the UI could show or remove. An edit is now a one-time patch that writes no such row, and a
merchant-level choice is a rule. This data-only migration turns each existing pin into an exact
`source='user'` rule on the merchant key, so it is visible and editable on the Rules screen, and
deletes the pin it replaces.

The converted rule is global (`user_id` NULL, like the pin it replaces: `merchant_map` has no
owner) and has a deliberately high priority number (`1000`, i.e. consulted after every ordinary
rule, which default to 50-100). That keeps exactly the precedence the pin had in the cascade: below
every rule of the user's, above the built-in rules and the cache. A merchant that already has an
enabled global exact user rule needs no new one (that rule already outranked the pin), so only the pin is
dropped.

Idempotent: once a pin has been converted it is gone, so a second run finds nothing to do (and a
database with no pins is untouched). The schema does not change. `downgrade` is a no-op: the rules
stay (older code honors user rules just as well) and the pins are not re-created.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_user_merchant_pins_to_rules"
down_revision: str | None = "0009_tax_profiles"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: Ranks the converted rule after every ordinary user rule, as the pin ranked below them.
_PIN_RULE_PRIORITY = 1000


def upgrade() -> None:
    bind = op.get_bind()
    pins = bind.execute(
        sa.text(
            "SELECT merchant_key, category_id, subcategory_id FROM merchant_map "
            "WHERE source = 'user' ORDER BY merchant_key"
        )
    ).all()
    now = datetime.now(UTC).isoformat()
    for merchant_key, category_id, subcategory_id in pins:
        # An empty key never matches a merchant (A27) and cannot be an exact rule.
        covered = merchant_key == "" or (
            bind.execute(
                sa.text(
                    "SELECT 1 FROM rules WHERE source = 'user' AND match_type = 'exact' "
                    "AND enabled = 1 AND pattern = :key AND user_id IS NULL LIMIT 1"
                ),
                {"key": merchant_key},
            ).first()
            is not None
        )
        if not covered:
            bind.execute(
                sa.text(
                    "INSERT INTO rules (user_id, match_type, pattern, category_id, "
                    "subcategory_id, kind_override, priority, enabled, source, created_at) "
                    "VALUES (NULL, 'exact', :key, :category_id, :subcategory_id, NULL, "
                    ":priority, 1, 'user', :now)"
                ),
                {
                    "key": merchant_key,
                    "category_id": category_id,
                    "subcategory_id": subcategory_id,
                    "priority": _PIN_RULE_PRIORITY,
                    "now": now,
                },
            )
        bind.execute(
            sa.text("DELETE FROM merchant_map WHERE merchant_key = :key AND source = 'user'"),
            {"key": merchant_key},
        )


def downgrade() -> None:
    """No-op: the converted rules stay and the pins they replaced are not re-created."""
