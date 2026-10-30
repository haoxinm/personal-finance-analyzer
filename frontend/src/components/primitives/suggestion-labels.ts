import type { components } from "@/api/client";

export type SuggestionCandidate = components["schemas"]["TransactionCandidate"];

export type CategoryLike = {
  key: string;
  name: string;
  subcategories: Array<{ key: string; name: string }>;
};

/** Where a suggestion comes from, as the chip words it (the rule's pattern is quoted). */
export function sourceLabel(candidate: SuggestionCandidate): string {
  switch (candidate.source) {
    case "one_time_edit":
      return "your edit";
    case "kind":
      return "payments & transfers";
    case "user_rule":
      return `your rule '${candidate.label}'`;
    case "builtin_rule":
      return "built-in";
    case "issuer":
      return "issuer";
    case "merchant_cache":
      return "merchant cache";
    case "llm":
      return "LLM (saved)";
  }
}

/** "Category › Subcategory", falling back to the raw keys for one the taxonomy does not list. */
export function categoryLabel(
  categories: CategoryLike[],
  categoryKey: string,
  subcategoryKey: string,
): string {
  const category = categories.find((c) => c.key === categoryKey);
  const subcategory = category?.subcategories.find((s) => s.key === subcategoryKey);
  return `${category?.name ?? categoryKey} › ${subcategory?.name ?? subcategoryKey}`;
}
