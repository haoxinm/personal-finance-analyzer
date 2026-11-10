"""The classification system prompt (§3.7, §3.8).

`PROMPT_VERSION` is a module constant recorded on every `llm_runs` row (I10): bump it whenever
`SYSTEM_PROMPT` changes, so a stored classification always says exactly which wording produced it.

The taxonomy block is generated from `taxonomy.yaml` (via `classify/taxonomy.py`) at import time,
so it cannot drift from the schema (`classify/llm/schema.py`) or the database seed.
"""

from __future__ import annotations

from personal_finance_analyzer.classify.taxonomy import CATEGORIES

#: Bump whenever `SYSTEM_PROMPT`'s wording changes. Recorded per `llm_runs` row (I10).
PROMPT_VERSION = "v1"


def _taxonomy_block() -> str:
    lines = []
    for cat in CATEGORIES:
        sub_keys = ", ".join(sub.key for sub in cat.subcategories)
        if cat.dynamic_subcategories:
            sub_keys += ", ... (dynamic: any lowercase_slug of the store name)"
        lines.append(f"- {cat.key}: {sub_keys}")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You classify payment-card and bank transaction descriptions into a fixed taxonomy.

INPUT: CSV with columns id,description. Descriptions may be in any language and are often
abbreviated, truncated, or contain merchant codes.

OUTPUT: JSON only. One item per input row, every id echoed exactly once. No prose.

RULES
- Exactly one category and one subcategory per row, from the list below.
- Output English only, including merchant_canonical, regardless of input language.
- online_shopping: subcategory is a slug of the store name (amazon, yami, temu). Set
  is_online_store=true. Use this only for online-first retailers.
- Restaurant vs delivery: DoorDash/Uber Eats/Grubhub/Instacart-from-a-restaurant => delivery.
- Wholesale clubs (Costco, Sam's Club, BJ's) => grocery/wholesale_clubs even though they sell
  everything.
- If a description is ambiguous or unrecognizable, use others/uncategorized with low confidence.
  Do not guess to appear helpful — low confidence routes it to human review, which is the correct
  outcome.
- confidence reflects certainty about the merchant identity AND the category fit.

TAXONOMY
{_taxonomy_block()}
"""
