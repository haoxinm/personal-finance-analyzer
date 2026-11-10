"""LLM response schema (§3.8, I4).

`Item.category` is the `CategoryKey` literal generated from `taxonomy.yaml` at import time (via
`classify/taxonomy.py`), so an out-of-taxonomy category is a `pydantic.ValidationError` at parse
time — it can never reach the database. `subcategory` is validated against the category's declared
subcategories, except for `online_shopping` (`taxonomy.is_dynamic`), where any lowercase
underscore-separated slug is accepted (e.g. a store name like `amazon`, `yami`, `temu`).

Per A8, `ClassificationBatch.model_json_schema()` is passed to the LLM provider as an explicit
dict — never the Pydantic class itself — so this module never marks optional fields required.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from personal_finance_analyzer.classify.taxonomy import CategoryKey, is_dynamic, subcategories_for

#: A dynamic (online_shopping) subcategory must look like a store-name slug: lowercase letters
#: and digits, words joined by a single underscore. No spaces, no punctuation.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


class Item(BaseModel):
    """One classified row, keyed by the ephemeral per-request `id` assigned in `build_csv`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    category: CategoryKey  # type: ignore[valid-type]  # see taxonomy.CategoryKey's own note
    subcategory: str
    #: English/Latin script, title case, e.g. 'Trader Joes'. Never egressed elsewhere; this is
    #: the LLM's own output, held only in memory by this module.
    merchant_canonical: str
    confidence: float = Field(ge=0.0, le=1.0)
    is_online_store: bool = False

    @model_validator(mode="after")
    def _check_subcategory(self) -> Item:
        if is_dynamic(self.category):
            if not _SLUG_RE.match(self.subcategory):
                raise ValueError(
                    f"subcategory {self.subcategory!r} is not a valid store slug for dynamic "
                    f"category {self.category!r}"
                )
        elif self.subcategory not in subcategories_for(self.category):
            raise ValueError(
                f"subcategory {self.subcategory!r} is not valid for category {self.category!r}"
            )
        return self


class ClassificationBatch(BaseModel):
    """The full response for one request: one `Item` per input row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[Item]
