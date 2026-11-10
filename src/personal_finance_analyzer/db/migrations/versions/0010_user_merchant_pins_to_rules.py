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
rule, which default to 50-100; the Rules screen lets the user change it, and any rule they create
later defaults to 100, so it stays below theirs). That keeps exactly the precedence the pin had in
the cascade: below every rule of the user's, above the built-in rules and the cache. A merchant that
already has an enabled global exact user rule (compared the way the cascade matches exact rules:
whitespace-stripped and case-folded) needs no new one (that rule already outranked the pin), so only
the pin is dropped. The `rules` table has no hit counter, so the pin's ``hit_count`` has nothing to
carry over to.

**Transactions the pins classified.** Before the pin became a rule, the cascade wrote
``classified_by='user'`` on every pin hit, the same value an explicit one-time edit writes. Stored
rows were never rewritten, so re-classify, "Apply to existing" and new rules skip them as if the
user had edited each one, and suggestions call them "your one-time edit". This migration therefore
sets ``classified_by='user_rule'`` (what the cascade writes for a rule hit) on the rows that a pin
classified, so they follow the converted rule from now on. Nothing in the data tells a pin hit from
an edit with certainty (both wrote an audit row with method ``user`` and confidence 1.0), so the
heuristic errs towards un-freezing, which is harmless because the converted rule gives the same
category: a row is left alone (a real edit) unless **all** of these hold:

- its merchant had a pin, and its category and subcategory equal the pin's target (an edit to
  anything else, and every edit made since pins stopped being written, is kept);
- the latest `classifications` entry of the row has method ``user`` (otherwise the row is not what
  ``classified_by`` claims; leave it);
- that entry was not written together with the pin: an explicit correction wrote the audit row and
  created or updated the pin in the same call, so an entry within `_SAME_CALL_SECONDS` of the pin's
  ``updated_at`` is the edit that made the pin (the last one per merchant stays a one-time edit; an
  earlier edit of the same merchant to the same category is un-frozen, which only changes its label).

Idempotent: once a pin has been converted it is gone, so a second run finds nothing to do (and a
database with no pins is untouched; the transactions are rewritten in the same step as their pin). The schema does not change. `downgrade` is a no-op: the rules
stay (older code honors user rules just as well) and the pins are not re-created.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_user_merchant_pins_to_rules"
down_revision: str | None = "0009_tax_profiles"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: Ranks the converted rule after every ordinary user rule, as the pin ranked below them.
_PIN_RULE_PRIORITY = 1000
#: An audit entry this close to the pin's ``updated_at`` was written by the same correction.
_SAME_CALL_SECONDS = 5


def _fold(text: str) -> str:
    """How the cascade compares an exact rule's pattern with a merchant key."""
    return text.strip().casefold()


def _parse(stamp: str | None) -> datetime | None:
    if stamp is None:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _unfreeze_pin_hits(
    bind: sa.Connection, key: str, category_id: int, subcategory_id: int, pin_updated: str | None
) -> None:
    """Set ``classified_by='user_rule'`` on the rows `key`'s pin classified (see the docstring)."""
    pinned_at = _parse(pin_updated)
    rows = bind.execute(
        sa.text(
            "SELECT t.id, c.created_at FROM transactions t "
            "JOIN classifications c ON c.id = ("
            "  SELECT c2.id FROM classifications c2 WHERE c2.transaction_id = t.id "
            "  ORDER BY c2.created_at DESC, c2.id DESC LIMIT 1) "
            "WHERE t.merchant_key = :key AND t.classified_by = 'user' "
            "AND t.category_id = :cat AND t.subcategory_id = :sub AND c.method = 'user'"
        ),
        {"key": key, "cat": category_id, "sub": subcategory_id},
    ).all()
    window = timedelta(seconds=_SAME_CALL_SECONDS)
    for txn_id, audited in rows:
        written = _parse(audited)
        if pinned_at is not None and written is not None and abs(written - pinned_at) <= window:
            continue  # the correction that made the pin: a real one-time edit
        bind.execute(
            sa.text("UPDATE transactions SET classified_by = 'user_rule' WHERE id = :id"),
            {"id": txn_id},
        )


def upgrade() -> None:
    bind = op.get_bind()
    pins = bind.execute(
        sa.text(
            "SELECT merchant_key, category_id, subcategory_id, updated_at FROM merchant_map "
            "WHERE source = 'user' ORDER BY merchant_key"
        )
    ).all()
    now = datetime.now(UTC).isoformat()
    covered_keys = {
        _fold(pattern)
        for (pattern,) in bind.execute(
            sa.text(
                "SELECT pattern FROM rules WHERE source = 'user' AND match_type = 'exact' "
                "AND enabled = 1 AND user_id IS NULL"
            )
        )
    }
    for merchant_key, category_id, subcategory_id, updated_at in pins:
        _unfreeze_pin_hits(bind, merchant_key, category_id, subcategory_id, updated_at)
        # An empty key never matches a merchant (A27) and cannot be an exact rule.
        covered = merchant_key == "" or _fold(merchant_key) in covered_keys
        if not covered:
            covered_keys.add(_fold(merchant_key))
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
