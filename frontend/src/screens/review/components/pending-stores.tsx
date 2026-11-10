import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, LoadingState } from "@/components/primitives/states";

import type { MergeTarget, UsePendingSubcategoriesResult } from "../use-pending-subcategories";

/**
 * "New categories to approve": dynamically-proposed subcategories (the plan calls them "pending
 * stores") awaiting approval, each rendered with the "approve / merge into ▾" picker the plan calls
 * for (§P3-B, e.g. "Yami — approve / merge into ▾"), keyed by `Subcategory.id`.
 */
export function PendingStores({
  pending,
  mergeTargets,
  approve,
  merge,
  isBusy,
  isLoading,
  isError,
  refetch,
}: UsePendingSubcategoriesResult) {
  const [openMergeFor, setOpenMergeFor] = React.useState<string | null>(null);

  if (isLoading) return <LoadingState rows={2} />;
  if (isError) {
    return <ErrorState title="Could not load new categories" onRetry={refetch} className="p-6" />;
  }
  if (pending.length === 0) {
    // One quiet line: this section sits below the queue and is usually empty.
    return <p className="text-sm text-muted-foreground">No new categories are waiting for approval.</p>;
  }

  return (
    <ul className="flex flex-col gap-2">
      {pending.map((row) => {
        const busy = isBusy(row.key);
        return (
          <li key={row.key}>
            <Card>
              <CardHeader className="flex-row items-center justify-between gap-2 py-3">
                <CardTitle className="flex items-center gap-2 text-sm font-medium">
                  {row.name}
                  <Badge variant="secondary" className="text-xs">
                    {row.categoryLabel}
                  </Badge>
                </CardTitle>
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={busy}
                    onClick={() => approve(row)}
                  >
                    Approve
                  </Button>
                  <div className="relative">
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      disabled={busy || mergeTargets.length === 0}
                      onClick={() => setOpenMergeFor((k) => (k === row.key ? null : row.key))}
                    >
                      Merge into ▾
                    </Button>
                    {openMergeFor === row.key ? (
                      <MergeMenu
                        targets={mergeTargets}
                        onChoose={(target) => {
                          setOpenMergeFor(null);
                          merge(row, target);
                        }}
                        onClose={() => setOpenMergeFor(null)}
                      />
                    ) : null}
                  </div>
                </div>
              </CardHeader>
            </Card>
          </li>
        );
      })}
    </ul>
  );
}

function MergeMenu({
  targets,
  onChoose,
  onClose,
}: {
  targets: MergeTarget[];
  onChoose: (target: MergeTarget) => void;
  onClose: () => void;
}) {
  return (
    <div
      role="menu"
      className="absolute right-0 z-10 mt-1 max-h-56 w-56 overflow-auto rounded-md border border-border bg-card p-1 shadow-lg"
    >
      <button
        type="button"
        className="mb-1 w-full rounded-sm px-2 py-1 text-left text-xs text-muted-foreground hover:bg-muted"
        onClick={onClose}
      >
        Cancel
      </button>
      {targets.map((target) => (
        <button
          key={target.key}
          type="button"
          role="menuitem"
          className="w-full rounded-sm px-2 py-1 text-left text-sm hover:bg-muted"
          onClick={() => onChoose(target)}
        >
          {target.label}
          <span className="ml-1 text-xs text-muted-foreground">({target.categoryLabel})</span>
        </button>
      ))}
    </div>
  );
}
