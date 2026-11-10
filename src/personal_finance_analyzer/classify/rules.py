"""Rule matching, built-in rule seeding, and the issuer-category map (§2b P2-B, D8, A16).

Two static files back this module, both loaded once at import time and validated against
`taxonomy.py` (an unknown category/subcategory key fails fast rather than silently mis-classifying
later):

- `builtin_rules.yaml` — cascade step 4. `sync_builtin_rules()` idempotently upserts these into
  the `rules` table with `source='builtin'` (mirroring `taxonomy.sync_taxonomy`'s pattern), so the
  cascade's rule lookups (steps 2 and 4) are a single, uniform `rules` query differing only in
  `source`. Never deletes a row, and never touches a `source='user'` row.

Every rule (user or builtin) is matched against `merchant_key`, never `description_raw` or
`description_clean`: `merchant_key` is already lowercased and stripped of store numbers and
redaction tokens (§3.6, A27), so a pattern like ``"STARBUCKS"`` matches every store number and
punctuation variant of the same merchant with one `contains` rule. `kinds.py`, by contrast,
matches against `description_clean`, because kind words ("PAYMENT", "FEE", "INTEREST") are not
merchant identity and `merchant_key` reduction is tuned for the latter, not the former.
- `issuer_category_map.yaml` — cascade step 5 (A16). A pure lookup, `(parser_id, label) ->
  (category, subcategory)`; never written to the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.classify.taxonomy import CATEGORY_KEYS, subcategories_for
from personal_finance_analyzer.core.errors import ConfigError
from personal_finance_analyzer.db.models import Category, Rule, Subcategory
from personal_finance_analyzer.ingest.normalize import normalize

_BUILTIN_RULES_RESOURCE = "builtin_rules.yaml"
_ISSUER_CATEGORY_MAP_RESOURCE = "issuer_category_map.yaml"

#: `Rule.match_type` values this module understands (§3.2 lists a third, `exact`, for
#: user-authored rules; no built-in rule uses it — see `builtin_rules.yaml`'s header).
_MATCH_TYPES = ("exact", "contains", "regex")


def _load_yaml_resource(name: str) -> object:
    text = (
        resources.files("personal_finance_analyzer.classify")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )
    return yaml.safe_load(text)


def rule_matches_text(match_type: str, pattern: str, merchant_key: str) -> bool:
    """True if `pattern` (of `match_type`) matches `merchant_key`.

    All matching is case-insensitive. `exact` compares the whole (stripped) string; `contains` is
    a substring test; `regex` is `re.search` with `re.IGNORECASE`.

    Raises:
        ConfigError: `match_type` is none of `exact`, `contains`, `regex`.
    """
    if match_type == "exact":
        return merchant_key.strip().casefold() == pattern.strip().casefold()
    if match_type == "contains":
        return pattern.casefold() in merchant_key.casefold()
    if match_type == "regex":
        return re.search(pattern, merchant_key, re.IGNORECASE) is not None
    raise ConfigError(f"unknown rule match_type {match_type!r}")


def rule_matches(rule: Rule, merchant_key: str) -> bool:
    """`rule_matches_text` against a persisted `Rule` row."""
    return rule_matches_text(rule.match_type, rule.pattern, merchant_key)


# ------------------------------------------------------------------------------------------------
# builtin_rules.yaml — cascade step 4 (D8).
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuiltinRuleDef:
    """One `builtin_rules.yaml` entry, validated against `taxonomy.py`."""

    pattern: str
    match_type: str  # "contains" | "regex" (no builtin rule uses "exact")
    category_key: str
    subcategory_key: str
    justification: str
    test_description: str


def load_builtin_rules() -> tuple[BuiltinRuleDef, ...]:
    """Load, validate, and self-test every `builtin_rules.yaml` entry.

    Each entry's `category`/`subcategory` must be a real taxonomy pair, its `match_type` must be
    one this module understands, and its own `pattern` must match the `merchant_key` that
    `ingest.normalize.normalize` derives from its `test_description` (i.e. exactly what the
    cascade would compute for a real transaction with that raw description) — a rule that fails
    its own example is almost certainly a typo, caught here rather than in production.

    Raises:
        ConfigError: any of the above checks fails, or the YAML is malformed.
    """
    raw = _load_yaml_resource(_BUILTIN_RULES_RESOURCE)
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{_BUILTIN_RULES_RESOURCE}: must be a non-empty list at the top level")

    rules: list[BuiltinRuleDef] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigError(
                f"{_BUILTIN_RULES_RESOURCE}: each entry must be a mapping, got {entry!r}"
            )
        try:
            pattern = str(entry["pattern"])
            match_type = str(entry["match_type"])
            category_key = str(entry["category"])
            subcategory_key = str(entry["subcategory"])
            justification = str(entry["justification"])
            test_description = str(entry["test_description"])
        except KeyError as exc:
            raise ConfigError(
                f"{_BUILTIN_RULES_RESOURCE}: entry missing required key {exc}: {entry!r}"
            ) from exc

        if match_type not in _MATCH_TYPES:
            raise ConfigError(f"{_BUILTIN_RULES_RESOURCE}: invalid match_type {match_type!r}")
        if category_key not in CATEGORY_KEYS:
            raise ConfigError(f"{_BUILTIN_RULES_RESOURCE}: unknown category {category_key!r}")
        if subcategory_key not in subcategories_for(category_key):
            raise ConfigError(
                f"{_BUILTIN_RULES_RESOURCE}: unknown subcategory {subcategory_key!r} for "
                f"category {category_key!r}"
            )
        if match_type == "regex":
            re.compile(pattern)  # fail fast on a broken pattern

        merchant_key = normalize(test_description, ()).merchant_key
        if not rule_matches_text(match_type, pattern, merchant_key):
            raise ConfigError(
                f"{_BUILTIN_RULES_RESOURCE}: rule {pattern!r} does not match the merchant_key "
                f"{merchant_key!r} derived from its own test_description {test_description!r}"
            )

        rules.append(
            BuiltinRuleDef(
                pattern=pattern,
                match_type=match_type,
                category_key=category_key,
                subcategory_key=subcategory_key,
                justification=justification,
                test_description=test_description,
            )
        )
    return tuple(rules)


#: Loaded and validated once at import time, exactly like `taxonomy.CATEGORIES` (P0-4's pattern).
BUILTIN_RULES: tuple[BuiltinRuleDef, ...] = load_builtin_rules()


def sync_builtin_rules(session: Session) -> None:
    """Idempotently upsert `BUILTIN_RULES` into the `rules` table with `source='builtin'`.

    Mirrors `taxonomy.sync_taxonomy`'s upsert pattern: matched by `(pattern, match_type)` among
    existing `source='builtin'` rows, never deletes a row, and never touches a `source='user'`
    row. Requires the taxonomy to already be synced (`sync_taxonomy`, P0-4) — every category and
    subcategory `BUILTIN_RULES` references must already exist.

    Called automatically, and cheaply (a handful of rows), at the start of every
    `cascade.classify_transactions` run, so callers never need to invoke it themselves.
    """
    existing = {
        (row.pattern, row.match_type): row
        for row in session.execute(select(Rule).where(Rule.source == "builtin")).scalars()
    }
    for defn in BUILTIN_RULES:
        category = session.execute(
            select(Category).where(Category.key == defn.category_key)
        ).scalar_one()
        subcategory = session.execute(
            select(Subcategory).where(
                Subcategory.category_id == category.id, Subcategory.key == defn.subcategory_key
            )
        ).scalar_one()

        row = existing.get((defn.pattern, defn.match_type))
        if row is None:
            session.add(
                Rule(
                    user_id=None,
                    match_type=defn.match_type,
                    pattern=defn.pattern,
                    category_id=category.id,
                    subcategory_id=subcategory.id,
                    kind_override=None,
                    priority=100,
                    enabled=True,
                    source="builtin",
                )
            )
        else:
            row.category_id = category.id
            row.subcategory_id = subcategory.id
    session.flush()


# ------------------------------------------------------------------------------------------------
# issuer_category_map.yaml — cascade step 5 (A16).
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssuerCategoryMatch:
    """An unambiguous `(category, subcategory)` implied by an issuer's own category label."""

    category_key: str
    subcategory_key: str


def _normalize_label(label: str) -> str:
    return " ".join(label.split()).casefold()


def _load_issuer_category_map() -> dict[str, dict[str, IssuerCategoryMatch]]:
    raw = _load_yaml_resource(_ISSUER_CATEGORY_MAP_RESOURCE)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{_ISSUER_CATEGORY_MAP_RESOURCE}: must be a mapping at the top level")

    result: dict[str, dict[str, IssuerCategoryMatch]] = {}
    for parser_id, labels in raw.items():
        if not isinstance(labels, dict):
            raise ConfigError(
                f"{_ISSUER_CATEGORY_MAP_RESOURCE}: entry for {parser_id!r} must be a mapping"
            )
        by_label: dict[str, IssuerCategoryMatch] = {}
        for label, mapping in labels.items():
            if not isinstance(mapping, dict):
                raise ConfigError(
                    f"{_ISSUER_CATEGORY_MAP_RESOURCE}: {parser_id!r}/{label!r} must map to a "
                    "mapping with 'category' and 'subcategory'"
                )
            category_key = str(mapping.get("category", ""))
            subcategory_key = str(mapping.get("subcategory", ""))
            if category_key not in CATEGORY_KEYS:
                raise ConfigError(
                    f"{_ISSUER_CATEGORY_MAP_RESOURCE}: {parser_id!r}/{label!r}: unknown "
                    f"category {category_key!r}"
                )
            if subcategory_key not in subcategories_for(category_key):
                raise ConfigError(
                    f"{_ISSUER_CATEGORY_MAP_RESOURCE}: {parser_id!r}/{label!r}: unknown "
                    f"subcategory {subcategory_key!r} for category {category_key!r}"
                )
            by_label[_normalize_label(str(label))] = IssuerCategoryMatch(
                category_key=category_key, subcategory_key=subcategory_key
            )
        result[str(parser_id)] = by_label
    return result


#: Loaded and validated once at import time.
ISSUER_CATEGORY_MAP: dict[str, dict[str, IssuerCategoryMatch]] = _load_issuer_category_map()


def resolve_issuer_category(parser_id: str | None, label: str | None) -> IssuerCategoryMatch | None:
    """Look up an unambiguous `(category, subcategory)` for `label` (A16).

    Tries `parser_id`'s own bucket first, then the parser-agnostic `"*"` bucket. `label` is
    matched case-insensitively with whitespace collapsed.

    Returns:
        The match, or `None` when there is no unambiguous entry for this `(parser_id, label)` —
        the caller (cascade step 5) falls through to the LLM. `label` is local-only (I1b) and is
        never included in any egress payload by this function or its callers.
    """
    if not label:
        return None
    normalized = _normalize_label(label)
    for key in (parser_id, "*"):
        if key is None:
            continue
        bucket = ISSUER_CATEGORY_MAP.get(key)
        if bucket is not None and normalized in bucket:
            return bucket[normalized]
    return None
