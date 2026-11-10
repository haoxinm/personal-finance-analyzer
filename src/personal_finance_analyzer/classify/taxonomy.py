"""Taxonomy source of truth and sync (§3.3, D7/A3, P0-4).

`taxonomy.yaml` is the **sole** source of truth for the two-level category/subcategory taxonomy.
This module loads and validates it at import time, and exposes everything derived from it — the
prompt, the LLM response schema's `CategoryKey` literal, the API enum, and the UI all build on
these exports rather than re-reading the YAML themselves, so they cannot drift from one another.

`sync_taxonomy()` is the only writer of `categories`/`subcategories` rows: migrations create those
tables empty (A3). It is idempotent and never deletes a row.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Literal

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.core.errors import ConfigError
from personal_finance_analyzer.db.models import Category, Subcategory

_YAML_RESOURCE = "taxonomy.yaml"


@dataclass(frozen=True, slots=True)
class SubcategoryDef:
    key: str
    label: str


@dataclass(frozen=True, slots=True)
class CategoryDef:
    key: str
    label: str
    sort_order: int
    dynamic_subcategories: bool
    subcategories: tuple[SubcategoryDef, ...]


def _humanize(key: str) -> str:
    return key.replace("_", " ").title()


def _load_raw() -> dict[str, object]:
    text = (
        resources.files("personal_finance_analyzer.classify")
        .joinpath(_YAML_RESOURCE)
        .read_text(encoding="utf-8")
    )
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ConfigError(f"{_YAML_RESOURCE} must contain a mapping at the top level")
    return data


def _parse_and_validate(raw: dict[str, object]) -> tuple[int, tuple[CategoryDef, ...]]:
    version = raw.get("version")
    if not isinstance(version, int):
        raise ConfigError(f"{_YAML_RESOURCE}: 'version' must be an integer")

    raw_categories = raw.get("categories")
    if not isinstance(raw_categories, list) or not raw_categories:
        raise ConfigError(f"{_YAML_RESOURCE}: 'categories' must be a non-empty list")

    categories: list[CategoryDef] = []
    seen_category_keys: set[str] = set()

    for sort_order, entry in enumerate(raw_categories):
        if not isinstance(entry, dict):
            raise ConfigError(f"{_YAML_RESOURCE}: each category must be a mapping, got {entry!r}")
        key = entry.get("key")
        label = entry.get("label")
        if not isinstance(key, str) or not key:
            raise ConfigError(f"{_YAML_RESOURCE}: category missing a string 'key': {entry!r}")
        if key in seen_category_keys:
            raise ConfigError(f"{_YAML_RESOURCE}: duplicate category key {key!r}")
        seen_category_keys.add(key)
        if not isinstance(label, str) or not label:
            raise ConfigError(f"{_YAML_RESOURCE}: category {key!r} missing a string 'label'")

        dynamic = bool(entry.get("dynamic_subcategories", False))
        if dynamic and key != "online_shopping":
            raise ConfigError(
                f"{_YAML_RESOURCE}: only 'online_shopping' may set dynamic_subcategories "
                f"(got {key!r})"
            )

        raw_subs = entry.get("subcategories")
        if not isinstance(raw_subs, list) or not raw_subs:
            # I4: every category has >= 1 subcategory.
            raise ConfigError(f"{_YAML_RESOURCE}: category {key!r} must declare >=1 subcategory")

        subs: list[SubcategoryDef] = []
        seen_sub_keys: set[str] = set()
        for sub_key in raw_subs:
            if not isinstance(sub_key, str) or not sub_key:
                raise ConfigError(
                    f"{_YAML_RESOURCE}: category {key!r} has a non-string subcategory key"
                )
            if sub_key in seen_sub_keys:
                raise ConfigError(
                    f"{_YAML_RESOURCE}: duplicate subcategory key {sub_key!r} in category {key!r}"
                )
            seen_sub_keys.add(sub_key)
            subs.append(SubcategoryDef(key=sub_key, label=_humanize(sub_key)))

        categories.append(
            CategoryDef(
                key=key,
                label=label,
                sort_order=sort_order,
                dynamic_subcategories=dynamic,
                subcategories=tuple(subs),
            )
        )

    if not any(
        cat.key == "others" and any(sub.key == "uncategorized" for sub in cat.subcategories)
        for cat in categories
    ):
        raise ConfigError(f"{_YAML_RESOURCE}: 'others/uncategorized' is mandatory and missing")

    return version, tuple(categories)


_raw = _load_raw()
TAXONOMY_VERSION, CATEGORIES = _parse_and_validate(_raw)

CATEGORY_KEYS: tuple[str, ...] = tuple(cat.key for cat in CATEGORIES)

#: Built at import time from `taxonomy.yaml`, so the LLM response schema, the API enum, and the UI
#: cannot drift from the taxonomy file. `mypy --strict` sees this as `str` (the runtime value is
#: rechecked by `_parse_and_validate`'s validation, not by the type checker), because a `Literal`
#: cannot be constructed dynamically in a way static tools can check element-by-element.
CategoryKey = Literal[tuple(CATEGORY_KEYS)]  # type: ignore[valid-type]

_CATEGORY_BY_KEY: dict[str, CategoryDef] = {cat.key: cat for cat in CATEGORIES}


def subcategories_for(category_key: str) -> tuple[str, ...]:
    """Return the subcategory keys declared for ``category_key``.

    Raises:
        KeyError: if ``category_key`` is not a known category.
    """
    return tuple(sub.key for sub in _CATEGORY_BY_KEY[category_key].subcategories)


def is_dynamic(category_key: str) -> bool:
    """Return whether ``category_key`` accepts subcategories beyond those declared in the YAML
    (only ``online_shopping`` in v1)."""
    return _CATEGORY_BY_KEY[category_key].dynamic_subcategories


def sync_taxonomy(session: Session) -> None:
    """Idempotently upsert `categories` and `subcategories` from `taxonomy.yaml` (D7/A3).

    Never deletes a row. Raises `ConfigError` if a category or subcategory key that is
    referenced by at least one transaction has been removed from the YAML.
    """
    existing_categories = {c.key: c for c in session.execute(select(Category)).scalars()}
    existing_subs: dict[tuple[str, str], Subcategory] = {}
    for sub in session.execute(select(Subcategory)).scalars():
        cat = session.get(Category, sub.category_id)
        if cat is not None:
            existing_subs[(cat.key, sub.key)] = sub

    yaml_category_keys = set(CATEGORY_KEYS)
    removed_categories = set(existing_categories) - yaml_category_keys
    for removed_key in removed_categories:
        cat = existing_categories[removed_key]
        if _category_is_referenced(session, cat.id):
            raise ConfigError(
                f"taxonomy.yaml no longer declares category {removed_key!r}, which is still "
                "referenced by at least one transaction"
            )

    for cat_def in CATEGORIES:
        category = existing_categories.get(cat_def.key)
        if category is None:
            category = Category(key=cat_def.key, label=cat_def.label, sort_order=cat_def.sort_order)
            session.add(category)
            session.flush()
        else:
            category.label = cat_def.label
            category.sort_order = cat_def.sort_order

        yaml_sub_keys = {sub.key for sub in cat_def.subcategories}
        for (existing_cat_key, existing_sub_key), sub_row in list(existing_subs.items()):
            if existing_cat_key != cat_def.key or existing_sub_key in yaml_sub_keys:
                continue
            if _subcategory_is_referenced(session, sub_row.id):
                raise ConfigError(
                    f"taxonomy.yaml no longer declares subcategory "
                    f"{cat_def.key}/{existing_sub_key!r}, which is still referenced by at least "
                    "one transaction"
                )

        for sub_def in cat_def.subcategories:
            existing_sub_row = existing_subs.get((cat_def.key, sub_def.key))
            if existing_sub_row is None:
                session.add(
                    Subcategory(category_id=category.id, key=sub_def.key, label=sub_def.label)
                )
            else:
                existing_sub_row.label = sub_def.label

    session.flush()


def _category_is_referenced(session: Session, category_id: int) -> bool:
    from personal_finance_analyzer.db.models import Transaction

    return (
        session.execute(
            select(Transaction.id).where(Transaction.category_id == category_id).limit(1)
        ).first()
        is not None
    )


def _subcategory_is_referenced(session: Session, subcategory_id: int) -> bool:
    from personal_finance_analyzer.db.models import Transaction

    return (
        session.execute(
            select(Transaction.id).where(Transaction.subcategory_id == subcategory_id).limit(1)
        ).first()
        is not None
    )
