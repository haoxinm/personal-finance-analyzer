import { cn } from "@/utils";

import {
  categoryLabel,
  sourceLabel,
  type CategoryLike,
  type SuggestionCandidate,
} from "./suggestion-labels";

export interface SuggestionChipsProps {
  candidates: SuggestionCandidate[];
  /** The sources disagree (the API's `conflict`): shows the explanation and highlights the box. */
  conflict: boolean;
  categories: CategoryLike[];
  /** One click: apply this candidate as a one-time edit of the transaction. */
  onApply: (candidate: SuggestionCandidate) => void;
  disabled?: boolean;
  className?: string;
}

/**
 * One chip per classification layer that has an answer for a transaction ("Grocery › Wholesale
 * clubs · built-in"), so a lower-priority answer can be used as a one-time edit with one click.
 * The highest-priority answer is already applied (it carries "Current") and still wins by
 * default; a click never changes a rule or the cache, only this transaction.
 *
 * Renders nothing when there is nothing to choose between (no candidates, or the only candidates
 * are the ones already applied).
 */
export function SuggestionChips({
  candidates,
  conflict,
  categories,
  onApply,
  disabled = false,
  className,
}: SuggestionChipsProps) {
  if (!candidates.some((candidate) => !candidate.is_current)) return null;

  return (
    <div
      role="group"
      aria-label="Suggested categories"
      data-testid="suggestion-chips"
      // Lets a screen with document-level key shortcuts (Review) leave Enter to a focused chip.
      data-suggestion-chips=""
      data-conflict={conflict ? "true" : "false"}
      className={cn(
        "flex flex-col gap-2 rounded-md border p-2",
        conflict ? "border-amber-500/60 bg-amber-500/10" : "border-border",
        className,
      )}
    >
      {conflict ? (
        <p role="note" className="text-xs text-foreground">
          Sources disagree — the highest-priority one is applied; click another to use it for this
          transaction only.
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">
          Other suggestions — click one to use it for this transaction only.
        </p>
      )}
      <ul className="flex flex-wrap gap-1.5">
        {candidates.map((candidate) => {
          const category = categoryLabel(
            categories,
            candidate.category_key,
            candidate.subcategory_key,
          );
          const source = sourceLabel(candidate);
          // A one-time edit that is already in place has nothing left to apply.
          const inert = candidate.source === "one_time_edit";
          return (
            <li key={`${candidate.source}:${candidate.rule_id ?? ""}:${candidate.label}`}>
              <button
                type="button"
                aria-pressed={candidate.is_current}
                aria-disabled={inert || disabled ? true : undefined}
                aria-label={`${category}, ${source}${candidate.is_current ? ", current" : ""}`}
                title={`${category} · ${source}${candidate.kind ? ` · sets kind to ${candidate.kind}` : ""}`}
                disabled={disabled}
                onClick={() => {
                  if (!inert) onApply(candidate);
                }}
                className={cn(
                  "inline-flex max-w-full items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs",
                  "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  candidate.is_current
                    ? "border-primary bg-primary/10 font-medium text-foreground"
                    : "border-border bg-transparent text-foreground hover:bg-muted",
                  disabled ? "opacity-60" : undefined,
                )}
              >
                <span className="truncate">
                  {category} · {source}
                </span>
                {candidate.is_current ? (
                  <span className="shrink-0 rounded-full bg-primary px-1.5 text-[10px] font-semibold uppercase tracking-wide text-primary-foreground">
                    Current
                  </span>
                ) : null}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
