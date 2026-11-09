import { Button } from "@/components/ui/button";
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
  /**
   * Hand an edited row back to automatic classification (clear its one-time edit and re-run the
   * rules). Only meaningful on a row that carries a one-time edit; when given, such a row shows a
   * "Revert to automatic" button, and its "Automatic" chip reverts instead of making a second edit.
   */
  onRevert?: () => void;
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
  onRevert,
  disabled = false,
  className,
}: SuggestionChipsProps) {
  const edited = candidates.some((candidate) => candidate.source === "one_time_edit");
  const canRevert = edited && onRevert !== undefined;
  if (!candidates.some((candidate) => !candidate.is_current) && !canRevert) return null;
  const automatic = candidates.find((candidate) => candidate.is_automatic);

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
      {edited ? (
        <p className="text-xs text-muted-foreground" data-testid="suggestion-edited-note">
          You edited this transaction, so rules and re-classifying leave it alone. Click another
          answer to use it for this transaction only, or revert to automatic.
        </p>
      ) : conflict ? (
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
          // The answer the row is filed under now has nothing left to apply: a click would only
          // freeze it as a one-time edit. It is shown, but it is not a control.
          if (candidate.is_current) {
            const why =
              candidate.source === "one_time_edit"
                ? "Your one-time edit. It stays until you revert to automatic."
                : "Already applied: this is the automatic answer for this transaction.";
            return (
              <li key={`${candidate.source}:${candidate.rule_id ?? ""}:${candidate.label}`}>
                <span
                  data-testid="current-chip"
                  title={`${category} · ${source}. ${why}`}
                  className="inline-flex max-w-full cursor-default items-center gap-1.5 rounded-full border border-primary bg-primary/10 px-2.5 py-1 text-xs font-medium text-foreground"
                >
                  <span className="truncate">
                    {category} · {source}
                  </span>
                  <span className="shrink-0 rounded-full bg-primary px-1.5 text-[10px] font-semibold uppercase tracking-wide text-primary-foreground">
                    Current
                  </span>
                  <span className="sr-only">. {why}</span>
                </span>
              </li>
            );
          }
          // On an edited row the winning layer is the "Automatic" answer, and clicking it hands
          // the row back (a second one-time edit would only re-freeze it).
          const revertsRow = canRevert && candidate.is_automatic;
          return (
            <li key={`${candidate.source}:${candidate.rule_id ?? ""}:${candidate.label}`}>
              <button
                type="button"
                aria-pressed={false}
                aria-label={`${revertsRow ? "Automatic: " : ""}${category}, ${source}${revertsRow ? ", reverts to automatic" : ""}`}
                title={
                  revertsRow
                    ? `Revert to automatic: ${category} · ${source}`
                    : `${category} · ${source}${candidate.kind ? ` · sets kind to ${candidate.kind}` : ""}`
                }
                disabled={disabled}
                onClick={() => {
                  if (revertsRow) onRevert?.();
                  else onApply(candidate);
                }}
                className={cn(
                  "inline-flex max-w-full items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs",
                  "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  "border-border bg-transparent text-foreground hover:bg-muted",
                  disabled ? "opacity-60" : undefined,
                )}
              >
                {edited && candidate.is_automatic ? (
                  <span className="shrink-0 rounded-full bg-muted px-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                    Automatic
                  </span>
                ) : null}
                <span className="truncate">
                  {category} · {source}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {canRevert ? (
        <div className="flex flex-col items-start gap-1">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={disabled}
            onClick={onRevert}
            title="Remove your one-time edit and let the rules decide again"
          >
            Revert to automatic
          </Button>
          {automatic === undefined ? (
            <p className="text-xs text-muted-foreground" data-testid="revert-fallback-note">
              No rule or saved answer matches this transaction, so it would become Uncategorized
              and be flagged for review.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
