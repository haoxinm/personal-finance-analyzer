"""The one place that gathers the user's personal-info entries for redaction: `[privacy]
pii_terms` merged with the statement user's `users.pii_aliases`, deduped. Used by the LLM
statement parser, the import pipeline's description normalization, the Layout mapper dry run and
the LLM preview, so every egress path redacts the same list."""

from __future__ import annotations

from sqlalchemy.orm import Session

from personal_finance_analyzer.config import Settings
from personal_finance_analyzer.core.pii import merge_entries, parse_aliases
from personal_finance_analyzer.db.models import User


def user_pii_terms(session: Session, settings: Settings, user_id: int | None) -> tuple[str, ...]:
    """`[privacy] pii_terms` + this user's `pii_aliases`, stripped and deduped."""
    aliases: list[str] = []
    user = session.get(User, user_id) if user_id is not None else None
    if user is not None:
        aliases = parse_aliases(user.pii_aliases)
    return merge_entries(settings.privacy.pii_terms, aliases)
