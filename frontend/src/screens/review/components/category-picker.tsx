import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/utils";

import type { Category } from "../types";
import { buildCategoryChoices } from "./category-choices";

export interface CategoryPickerProps {
  categories: Category[];
  stagedCategoryKey?: string;
  stagedSubcategoryKey?: string;
  onPick: (categoryKey: string, subcategoryKey: string) => void;
  disabled?: boolean;
}

/**
 * The number-key category strip and, for whichever category is currently staged, its
 * subcategory chips for a one-click refinement. Digits themselves are handled by `index.tsx`'s
 * single document-level keydown listener (§P3-B) — this component only renders the mapping
 * and reacts to clicks, so there is one source of truth for "what does `3` do right now".
 */
export function CategoryPicker({
  categories,
  stagedCategoryKey,
  stagedSubcategoryKey,
  onPick,
  disabled = false,
}: CategoryPickerProps) {
  const choices = React.useMemo(() => buildCategoryChoices(categories), [categories]);
  const stagedCategory = categories.find((cat) => cat.key === stagedCategoryKey);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2" role="group" aria-label="Category quick picks">
        {choices.map((choice, position) => (
          <Button
            key={choice.categoryKey}
            type="button"
            size="sm"
            variant={choice.categoryKey === stagedCategoryKey ? "default" : "outline"}
            disabled={disabled}
            onClick={() => onPick(choice.categoryKey, choice.subcategoryKey)}
          >
            <Badge variant="secondary" className="mr-1 px-1.5 py-0 text-xs">
              {position + 1}
            </Badge>
            {choice.categoryLabel}
          </Button>
        ))}
      </div>
      {stagedCategory ? (
        <div className="flex flex-wrap gap-1.5 border-l-2 border-border pl-3" aria-label="Refine subcategory">
          {stagedCategory.subcategories
            .filter((sub) => !sub.pending && sub.merged_into == null)
            .map((sub) => (
              <button
                key={sub.key}
                type="button"
                disabled={disabled}
                onClick={() => onPick(stagedCategory.key, sub.key)}
                className={cn(
                  "rounded-full border px-2.5 py-0.5 text-xs transition-colors",
                  sub.key === stagedSubcategoryKey
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-border bg-transparent text-muted-foreground hover:bg-muted",
                )}
              >
                {sub.name}
              </button>
            ))}
        </div>
      ) : null}
    </div>
  );
}
