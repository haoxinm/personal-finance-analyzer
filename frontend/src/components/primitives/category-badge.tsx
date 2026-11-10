import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/utils";

export interface CategoryBadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  /** The taxonomy category key, e.g. "groceries" (§3.3). Drives the colour deterministically. */
  categoryKey: string;
  /** Display label; falls back to a title-cased `categoryKey`. */
  label?: string;
  /** A subcategory to show alongside the category, e.g. "groceries / warehouse-club". */
  subcategoryLabel?: string;
  /** Marks a transaction awaiting classification review. */
  needsReview?: boolean;
}

// A fixed, deliberately non-default palette (P1-F: "a personal finance tool people will look
// at daily"), assigned by a stable hash of the category key so the same category always gets
// the same colour across sessions and screens, without a hand-maintained lookup table that
// falls out of sync with `taxonomy.yaml` (§3.3, owned by P0-4 / Phase 3's rules screen).
const PALETTE = [
  "bg-rose-100 text-rose-900 border-rose-200 dark:bg-rose-950 dark:text-rose-200 dark:border-rose-900",
  "bg-amber-100 text-amber-900 border-amber-200 dark:bg-amber-950 dark:text-amber-200 dark:border-amber-900",
  "bg-lime-100 text-lime-900 border-lime-200 dark:bg-lime-950 dark:text-lime-200 dark:border-lime-900",
  "bg-emerald-100 text-emerald-900 border-emerald-200 dark:bg-emerald-950 dark:text-emerald-200 dark:border-emerald-900",
  "bg-cyan-100 text-cyan-900 border-cyan-200 dark:bg-cyan-950 dark:text-cyan-200 dark:border-cyan-900",
  "bg-sky-100 text-sky-900 border-sky-200 dark:bg-sky-950 dark:text-sky-200 dark:border-sky-900",
  "bg-violet-100 text-violet-900 border-violet-200 dark:bg-violet-950 dark:text-violet-200 dark:border-violet-900",
  "bg-fuchsia-100 text-fuchsia-900 border-fuchsia-200 dark:bg-fuchsia-950 dark:text-fuchsia-200 dark:border-fuchsia-900",
] as const;

function hashKey(key: string): number {
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) {
    hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  }
  return hash;
}

function paletteClassFor(categoryKey: string): string {
  return PALETTE[hashKey(categoryKey) % PALETTE.length] ?? PALETTE[0];
}

function titleCase(key: string): string {
  return key
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(" ");
}

export function CategoryBadge({
  categoryKey,
  label,
  subcategoryLabel,
  needsReview = false,
  className,
  ...props
}: CategoryBadgeProps) {
  const text = label ?? titleCase(categoryKey);
  const fullText = subcategoryLabel ? `${text} / ${subcategoryLabel}` : text;

  return (
    <span className={cn("flex w-full min-w-0 items-center gap-1.5", className)} {...props}>
      <Badge
        variant="outline"
        title={fullText}
        className={cn("min-w-0 flex-1 border font-medium", paletteClassFor(categoryKey))}
      >
        <span className="block truncate">
          {text}
          {subcategoryLabel ? <span className="opacity-70"> / {subcategoryLabel}</span> : null}
        </span>
      </Badge>
      {needsReview ? (
        <Badge
          variant="secondary"
          title="Needs review"
          className="flex-shrink-0 whitespace-nowrap text-xs uppercase tracking-wide"
        >
          Review
        </Badge>
      ) : null}
    </span>
  );
}
