import { useQuery } from "@tanstack/react-query";
import * as React from "react";
import { Link } from "react-router-dom";

import { useSettings, useTaxonomy } from "@/api/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Kbd } from "@/components/primitives/kbd";
import { SuggestionChips } from "@/components/primitives/suggestion-chips";
import type { SuggestionCandidate } from "@/components/primitives/suggestion-labels";
import { Page, PageHeader } from "@/components/primitives/page";
import { EmptyState, ErrorState, LoadingState } from "@/components/primitives/states";
import { CategoryBadge } from "@/components/primitives/category-badge";
import { Money } from "@/components/primitives/money";
import { usePaneShortcutsEnabled, useSelectedUser } from "@/hooks/use-selected-user";

import { fetchUncategorizedCount } from "./api";
import { buildCategoryChoices } from "./components/category-choices";
import { CategoryPicker } from "./components/category-picker";
import { PendingStores } from "./components/pending-stores";
import { usePendingSubcategories } from "./use-pending-subcategories";
import { useReclassify } from "./use-reclassify";
import { useReviewQueue } from "./use-review-queue";
import type { Transaction } from "./types";

/**
 * The review screen (§P3-B) — "the highest-value screen": a keyboard-driven queue of
 * `needs_review` transactions, plus a panel for `pending_approval` "stores" (dynamically
 * proposed subcategories) to approve or merge.
 *
 * Keys (ignored while a text input has focus, or with a modifier held, so they never fight
 * the browser or a future search box):
 *   - `j` / `k`     move to the next / previous transaction in the queue
 *   - `1`-`9`       stage that quick-pick category (and its default subcategory) on the
 *                   current transaction — click a chip afterwards to refine the subcategory
 *   - `Enter`       accept the current transaction: the staged pick, or the cascade's own
 *                   proposal if nothing was staged. A one-time edit of this transaction only:
 *                   nothing is remembered about the merchant
 *   - `r`           accept, and also make it the merchant's standing rule ("always categorize
 *                   this merchant this way": future imports follow it with no review)
 *   - `u`           undo the single most recent accept (rows, and the rule if it created one)
 *
 * When a transaction's classification sources disagree (a rule, the built-in rules, the issuer's
 * label, the saved merchant answer), the card lists each one as a chip (`SuggestionChips`): one
 * click accepts that candidate as a one-time edit of this transaction only (undo with `u`).
 *
 * An accept also applies, as further one-time edits, to the other queue rows from the same
 * merchant (a checkbox on the card, on by default), and "Re-classify uncategorized" in the header
 * re-runs the cascade over rows that are still uncategorized, which is how a rule or model set up
 * after an import reaches the transactions already imported. A transaction you accepted yourself
 * is never changed by a re-classify or by any rule.
 *
 * The queue is built via `GET /transactions?needs_review=true` (`api.ts`'s
 * `fetchNeedsReviewQueue`); `description_raw` renders alongside `description_clean` (see
 * `RawDescription` below) so the user can see exactly what was inferred from what; and the
 * pending-stores panel's approve/merge actions call `/taxonomy/subcategories/{id}/...` using
 * `Subcategory.id` (see `use-pending-subcategories.ts`).
 */
export default function ReviewScreen() {
  const sel = useSelectedUser();
  // In compare mode only the active pane handles the document-level shortcuts.
  const shortcutsEnabled = usePaneShortcutsEnabled();
  const queue = useReviewQueue(sel.userIds, !sel.isLoading);
  const taxonomy = useTaxonomy();
  const pendingSubcategories = usePendingSubcategories();
  const settings = useSettings();
  const reclassify = useReclassify(sel.selectedUserId, queue.refetch);
  // Resolved only once the settings load: an unknown setting never shows the "LLM is off" hint.
  const llmOff = settings.data !== undefined && settings.data.llm.mode === "none";
  const userKey = sel.userIds?.join(",");
  const uncategorized = useQuery({
    queryKey: ["review", "uncategorized-count", userKey],
    enabled: !sel.isLoading,
    queryFn: () => fetchUncategorizedCount(sel.userIds),
  });
  const uncategorizedCount = uncategorized.data ?? 0;

  const categories = React.useMemo(() => taxonomy.data ?? [], [taxonomy.data]);
  const choices = React.useMemo(() => buildCategoryChoices(categories), [categories]);

  React.useEffect(() => {
    if (!shortcutsEnabled) return;
    function isTypingTarget(target: EventTarget | null): boolean {
      if (!(target instanceof HTMLElement)) return false;
      return target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable;
    }

    function onKeyDown(event: KeyboardEvent) {
      if (event.defaultPrevented) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTypingTarget(event.target)) return;
      // Enter on a focused suggestion chip is that chip's own click, not "accept the proposal".
      if (
        event.key === "Enter" &&
        event.target instanceof HTMLElement &&
        event.target.closest("[data-suggestion-chips]")
      ) {
        return;
      }

      if (event.key === "j") {
        event.preventDefault();
        queue.next();
        return;
      }
      if (event.key === "k") {
        event.preventDefault();
        queue.previous();
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        void queue.accept();
        return;
      }
      if (event.key === "r") {
        event.preventDefault();
        void queue.accept({ createRule: true });
        return;
      }
      if (event.key === "u") {
        event.preventDefault();
        void queue.undo();
        return;
      }
      const digit = Number(event.key);
      if (Number.isInteger(digit) && digit >= 1 && digit <= 9) {
        const choice = choices[digit - 1];
        if (choice) {
          event.preventDefault();
          queue.stage(choice.categoryKey, choice.subcategoryKey);
        }
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [queue, choices, shortcutsEnabled]);

  return (
    <Page>
      <PageHeader
        title="Review transactions"
        description={queue.status === "ready" ? `${queue.queue.length} need review` : "Loading…"}
        actions={
          <>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={reclassify.running || queue.isMutating}
              title="Re-runs your rules and the classifier over uncategorized and flagged transactions. Anything you categorized yourself is left alone."
              onClick={reclassify.start}
            >
              {reclassify.running ? "Re-classifying…" : "Re-classify uncategorized"}
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-label="Undo"
              aria-keyshortcuts="u"
              disabled={!queue.canUndo || queue.isMutating}
              onClick={() => void queue.undo()}
            >
              Undo
              <Kbd>u</Kbd>
            </Button>
          </>
        }
      />

      {llmOff && uncategorizedCount >= MANY_UNCATEGORIZED ? (
        <p
          role="note"
          data-testid="llm-off-hint"
          className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm text-foreground"
        >
          No LLM is set up, so {uncategorizedCount.toLocaleString()} transactions could not be
          categorized automatically. Add a rule for a recurring merchant, work through them here, or{" "}
          <Link to="/settings" className="underline">
            connect a local or remote model in Settings
          </Link>{" "}
          and then re-classify.
        </p>
      ) : null}

      <section aria-label="Transaction queue">
        {queue.status === "loading" ? <LoadingState rows={4} /> : null}
        {queue.status === "error" ? (
          <ErrorState
            title="Could not load the review queue"
            description={queue.error instanceof Error ? queue.error.message : undefined}
            onRetry={queue.refetch}
          />
        ) : null}
        {queue.status === "ready" && queue.queue.length === 0 ? (
          <EmptyState
            title="Nothing needs review"
            description="Every transaction has a confident category."
            action={
              <div className="flex flex-wrap justify-center gap-2 pt-1">
                <Button asChild size="sm" variant="outline">
                  <Link to="/spending/import">Import statements</Link>
                </Button>
                <Button asChild size="sm" variant="outline">
                  <Link to="/spending/transactions">Browse transactions</Link>
                </Button>
              </div>
            }
          />
        ) : null}
        {queue.status === "ready" && queue.current ? (
          <TransactionCard
            transaction={queue.current}
            position={queue.index}
            total={queue.queue.length}
            categories={categories}
            stagedCategoryKey={queue.stagedForCurrent?.categoryKey}
            stagedSubcategoryKey={queue.stagedForCurrent?.subcategoryKey}
            onStage={queue.stage}
            similarCount={queue.similarForCurrent.length}
            applyToSimilar={queue.applyToSimilar}
            onApplyToSimilarChange={queue.setApplyToSimilar}
            onAccept={() => void queue.accept()}
            onAcceptAndCreateRule={() => void queue.accept({ createRule: true })}
            onApplySuggestion={(candidate) =>
              void queue.accept({
                suggestion: {
                  categoryKey: candidate.category_key,
                  subcategoryKey: candidate.subcategory_key,
                  kind: candidate.kind ?? undefined,
                },
              })
            }
            disabled={queue.isMutating}
          />
        ) : null}
      </section>

      <section aria-labelledby="new-categories-heading" className="flex flex-col gap-3">
        <h2 id="new-categories-heading" className="text-sm font-semibold tracking-tight text-foreground">
          New categories to approve
        </h2>
        <p className="text-xs text-muted-foreground">Categories are shared by everyone, so this list is not filtered by user.</p>
        <PendingStores {...pendingSubcategories} />
      </section>
    </Page>
  );
}

export const screenMeta = { path: "/spending/review", title: "Review" };

/** How many uncategorized transactions it takes before the header explains that no LLM is set up. */
const MANY_UNCATEGORIZED = 10;

interface TransactionCardProps {
  transaction: Transaction;
  position: number;
  total: number;
  categories: Parameters<typeof CategoryPicker>[0]["categories"];
  stagedCategoryKey?: string;
  stagedSubcategoryKey?: string;
  onStage: (categoryKey: string, subcategoryKey: string) => void;
  /** Other queue rows from the same merchant, and whether accepting also applies to them. */
  similarCount: number;
  applyToSimilar: boolean;
  onApplyToSimilarChange: (apply: boolean) => void;
  onAccept: () => void;
  onAcceptAndCreateRule: () => void;
  /** One click on a suggestion chip: accept that candidate as a one-time edit of this row only. */
  onApplySuggestion: (candidate: SuggestionCandidate) => void;
  disabled: boolean;
}

function TransactionCard({
  transaction,
  position,
  total,
  categories,
  stagedCategoryKey,
  stagedSubcategoryKey,
  onStage,
  similarCount,
  applyToSimilar,
  onApplyToSimilarChange,
  onAccept,
  onAcceptAndCreateRule,
  onApplySuggestion,
  disabled,
}: TransactionCardProps) {
  const effectiveCategoryKey = stagedCategoryKey ?? transaction.category_key ?? undefined;
  const effectiveSubcategoryKey = stagedSubcategoryKey ?? transaction.subcategory_key ?? undefined;
  // A rule matches on the merchant key; a row without one has nothing to build a rule from.
  const ruleLabel = transaction.merchant_key;

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div>
          <CardTitle className="text-base font-semibold">{transaction.description_clean}</CardTitle>
          <RawDescription transaction={transaction} />
          <p className="mt-1 text-xs text-muted-foreground">
            {transaction.posted_date} · #{position + 1} of {total}
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <Money minorUnits={transaction.amount_minor} currency={transaction.currency} className="text-lg font-semibold" />
          {effectiveCategoryKey ? (
            <CategoryBadge categoryKey={effectiveCategoryKey} needsReview={transaction.needs_review} />
          ) : (
            <Badge variant="secondary">Uncategorized</Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <CategoryPicker
          categories={categories}
          stagedCategoryKey={effectiveCategoryKey}
          stagedSubcategoryKey={effectiveSubcategoryKey}
          onPick={onStage}
          disabled={disabled}
        />
        <SuggestionChips
          candidates={transaction.candidates ?? []}
          conflict={transaction.conflict ?? false}
          categories={categories}
          onApply={onApplySuggestion}
          disabled={disabled}
        />
        {similarCount > 0 ? (
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            <input
              type="checkbox"
              checked={applyToSimilar}
              onChange={(event) => onApplyToSimilarChange(event.target.checked)}
            />
            Also apply to {similarCount} other pending transaction{similarCount === 1 ? "" : "s"} from
            this merchant
          </label>
        ) : null}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            onClick={onAccept}
            disabled={disabled}
            aria-label="Accept"
            aria-keyshortcuts="Enter"
            title="Accepts this transaction only. Nothing is remembered about the merchant."
          >
            Accept
            <Kbd>Enter</Kbd>
          </Button>
          <Button
            type="button"
            variant="secondary"
            onClick={onAcceptAndCreateRule}
            disabled={disabled || ruleLabel === ""}
            aria-label={ruleLabel === "" ? "Always categorize this way" : `Always categorize “${ruleLabel}” this way`}
            aria-keyshortcuts="r"
            title="Accepts this one and saves a rule: every future import of this merchant is categorized this way automatically. Transactions you already edited by hand are never changed by it."
          >
            {ruleLabel === "" ? "Always categorize this way" : `Always categorize “${ruleLabel}” this way`}
            <Kbd>r</Kbd>
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          Accept changes this transaction only; nothing is remembered about the merchant. The
          second button also saves a rule, so future imports of this merchant need no review (see
          Rules). Transactions you accept yourself are never changed by a rule or a re-classify.
        </p>
      </CardContent>
    </Card>
  );
}

/** Renders `description_raw` next to `description_clean`, as the plan asks (§P3-B). */
function RawDescription({ transaction }: { transaction: Transaction }) {
  if (transaction.description_raw === transaction.description_clean) return null;
  return (
    <p className="text-xs text-muted-foreground">
      Raw: <span className="font-mono">{transaction.description_raw}</span>
    </p>
  );
}
