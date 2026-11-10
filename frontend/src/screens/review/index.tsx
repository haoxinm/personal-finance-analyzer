import * as React from "react";

import { useTaxonomy } from "@/api/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/primitives/states";
import { CategoryBadge } from "@/components/primitives/category-badge";
import { Money } from "@/components/primitives/money";
import { usePaneShortcutsEnabled, useSelectedUser } from "@/hooks/use-selected-user";

import { buildCategoryChoices } from "./components/category-choices";
import { CategoryPicker } from "./components/category-picker";
import { PendingStores } from "./components/pending-stores";
import { usePendingSubcategories } from "./use-pending-subcategories";
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
 *                   proposal if nothing was staged
 *   - `r`           accept, and also promote it into a rule (`TransactionPatch.create_rule`)
 *   - `u`           undo the single most recent accept
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
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Review</h1>
          <p className="text-sm text-muted-foreground">
            {queue.status === "ready"
              ? `${queue.queue.length} need review${sel.users.length > 1 && sel.selectedUser ? ` (${sel.selectedUser.name})` : ""}`
              : "Loading…"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" variant="outline" size="sm" disabled={!queue.canUndo || queue.isMutating} onClick={() => void queue.undo()}>
            Undo (u)
          </Button>
        </div>
      </header>

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
          <EmptyState title="Nothing needs review" description="Every transaction has a confident category." />
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
            onAccept={() => void queue.accept()}
            onAcceptAndCreateRule={() => void queue.accept({ createRule: true })}
            disabled={queue.isMutating}
          />
        ) : null}
      </section>

      <section aria-label="Pending stores" className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold tracking-tight text-foreground">Pending stores</h2>
        <p className="text-xs text-muted-foreground">Categories are shared by everyone, so this list is not filtered by user.</p>
        <PendingStores {...pendingSubcategories} />
      </section>
    </div>
  );
}

export const screenMeta = { path: "/spending/review", title: "Review" };

interface TransactionCardProps {
  transaction: Transaction;
  position: number;
  total: number;
  categories: Parameters<typeof CategoryPicker>[0]["categories"];
  stagedCategoryKey?: string;
  stagedSubcategoryKey?: string;
  onStage: (categoryKey: string, subcategoryKey: string) => void;
  onAccept: () => void;
  onAcceptAndCreateRule: () => void;
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
  onAccept,
  onAcceptAndCreateRule,
  disabled,
}: TransactionCardProps) {
  const effectiveCategoryKey = stagedCategoryKey ?? transaction.category_key ?? undefined;
  const effectiveSubcategoryKey = stagedSubcategoryKey ?? transaction.subcategory_key ?? undefined;

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
        <div className="flex gap-2">
          <Button type="button" onClick={onAccept} disabled={disabled}>
            Accept (Enter)
          </Button>
          <Button type="button" variant="secondary" onClick={onAcceptAndCreateRule} disabled={disabled}>
            Accept + create rule (r)
          </Button>
        </div>
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
