import * as React from "react";

import { usePatchTransaction, useTaxonomy } from "@/api/hooks";
import {
  useApplySuggestion,
  useRevertToAutomatic,
  useTransactionCandidates,
} from "@/api/suggestions";
import { categoryLabel, type SuggestionCandidate } from "@/components/primitives/suggestion-labels";
import { Page, PageHeader } from "@/components/primitives/page";
import { EmptyState, ErrorState, LoadingState } from "@/components/primitives/states";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/use-toast";
import { useSelectedUser } from "@/hooks/use-selected-user";
import { useSpendQueryParams } from "@/hooks/use-spend-query-params";

import {
  downloadTransactionsExport,
  useAccountOptions,
  useAllMatchingIds,
  useBulkUpdateTransactions,
  useTransactionsGrid,
} from "./api";
import { BulkBar } from "./bulk-bar";
import type { ColumnsContext } from "./columns";
import { EditPopover } from "./edit-popover";
import { FiltersBar } from "./filters";
import { TransactionsTable } from "./table";
import type { Transaction } from "./types";
import { useGridParams } from "./use-grid-params";

/**
 * The Transactions screen (P3-C): a virtualized grid of every transaction matching the URL's
 * `SpendQuery`-shaped filters (plus needs-review / uncategorized / one statement, see
 * `use-grid-params.ts`), sortable by date or amount, with inline single-row edit, bulk relabel over
 * the filtered selection, and a CSV/JSON export of exactly what the filters show. See `use-virtual-rows.ts` for why the "virtualized"
 * part is a hand-rolled windowing hook rather than `@tanstack/react-virtual` (not yet a
 * dependency — flagged in the P3-C report).
 */
export default function TransactionsScreen() {
  const [urlFilters, setFilters] = useSpendQueryParams();
  const [gridParams, setGridParams] = useGridParams();
  const sel = useSelectedUser();
  const { toast } = useToast();

  // The user switcher wins over a legacy `?user_ids=` param.
  const filters = React.useMemo(
    () => ({ ...urlFilters, userIds: sel.userIds ?? urlFilters.userIds }),
    [urlFilters, sel.userIds],
  );

  const grid = useTransactionsGrid(filters, gridParams, !sel.isLoading);
  const accountOptions = useAccountOptions();
  const accounts = React.useMemo(
    () =>
      (accountOptions.data ?? []).filter(
        (account) => sel.userIds === undefined || sel.userIds.includes(account.userId),
      ),
    [accountOptions.data, sel.userIds],
  );
  const taxonomy = useTaxonomy();
  const categories = React.useMemo(() => taxonomy.data ?? [], [taxonomy.data]);

  const patchTransaction = usePatchTransaction();
  const bulkUpdate = useBulkUpdateTransactions();
  const allMatchingIds = useAllMatchingIds();

  const [selectedIds, setSelectedIds] = React.useState<Set<number>>(new Set());
  // The selection is "everything the filters match" (from `handleSelectAllMatching`), not rows
  // the user ticked: bulk apply then asks for confirmation. Any manual change ends it.
  const [allMatchingSelected, setAllMatchingSelected] = React.useState(false);
  const [editing, setEditing] = React.useState<Transaction | null>(null);
  // After the editor closes, its opener (the row's Edit button) may unmount because the saved row
  // left the filter; focus then falls back to the grid container instead of <body>.
  const gridFocusRef = React.useRef<HTMLDivElement>(null);
  const restoreFocus = React.useRef(false);
  const wasEditing = React.useRef(false);
  const applySuggestion = useApplySuggestion();
  const revertToAutomatic = useRevertToAutomatic();
  const candidates = useTransactionCandidates(editing?.id);
  const [exporting, setExporting] = React.useState(false);

  // A selection made under one user view must not carry into another.
  React.useEffect(() => {
    setSelectedIds(new Set());
    setAllMatchingSelected(false);
  }, [sel.selectedUserId]);

  // A selection (ticked rows, or "all matching") describes the rows one particular filter set
  // matched; once the filters change it would silently target rows the user can no longer see.
  // Sort does not change the matched set, so it is left out of the key.
  const filterKey = JSON.stringify({ filters, grid: { ...gridParams, sort: undefined } });
  React.useEffect(() => {
    setSelectedIds(new Set());
    setAllMatchingSelected(false);
  }, [filterKey]);

  const rows = React.useMemo(
    () => grid.data?.pages.flatMap((page) => page.items) ?? [],
    [grid.data],
  );
  const total = grid.data?.pages.at(-1)?.total ?? 0;

  React.useEffect(() => {
    if (editing) {
      wasEditing.current = true;
      return;
    }
    if (wasEditing.current) {
      // Just closed: from now until the user's next pointer press, keep focus out of <body>.
      wasEditing.current = false;
      restoreFocus.current = true;
    }
    if (!restoreFocus.current) return;
    const active = document.activeElement;
    if (!active || active === document.body) gridFocusRef.current?.focus();
  }, [editing, rows]);
  React.useEffect(() => {
    // Once the user moves focus themselves (pointer), stop restoring it.
    const stop = () => {
      restoreFocus.current = false;
    };
    document.addEventListener("pointerdown", stop);
    return () => document.removeEventListener("pointerdown", stop);
  }, []);

  const toggleRow = React.useCallback((id: number) => {
    setAllMatchingSelected(false);
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const columnsContext: ColumnsContext = React.useMemo(
    () => ({
      categories,
      selectedIds,
      onToggleRow: toggleRow,
      onEdit: setEditing,
    }),
    [categories, selectedIds, toggleRow],
  );

  async function handleSelectAllMatching() {
    try {
      const ids = await allMatchingIds.mutateAsync({ filters, grid: gridParams });
      setSelectedIds(new Set(ids));
      setAllMatchingSelected(true);
      if (ids.length !== total) {
        // The list moved under us (an import finished, a row was relabelled): say what was selected.
        toast({
          title: `Selected ${ids.length.toLocaleString()} transactions`,
          description: `The filters matched ${total.toLocaleString()} a moment ago.`,
        });
      }
    } catch (error) {
      toast({
        title: "Could not select all matching transactions",
        description: error instanceof Error ? error.message : undefined,
        variant: "destructive",
      });
    }
  }

  async function handleBulkApply(patch: { categoryKey: string; subcategoryKey: string }) {
    const requested = selectedIds.size;
    try {
      const result = await bulkUpdate.mutateAsync({
        transactionIds: Array.from(selectedIds),
        categoryKey: patch.categoryKey,
        subcategoryKey: patch.subcategoryKey,
      });
      if (result.updated < requested) {
        // Never report a partial update as a success: some ids no longer exist.
        toast({
          title: `Updated ${result.updated.toLocaleString()} of ${requested.toLocaleString()} transactions`,
          description: `${(requested - result.updated).toLocaleString()} could not be updated (they may have been deleted). Reload to see the current list.`,
          variant: "destructive",
        });
      } else {
        toast({ title: `Updated ${result.updated} transaction${result.updated === 1 ? "" : "s"}` });
      }
      setSelectedIds(new Set());
      setAllMatchingSelected(false);
    } catch (error) {
      toast({
        title: "Bulk update failed",
        description: error instanceof Error ? error.message : undefined,
        variant: "destructive",
      });
    }
  }

  async function handleSaveEdit(patch: {
    categoryKey?: string;
    subcategoryKey?: string;
    kind?: Transaction["kind"];
    notes?: string;
    createRule?: boolean;
  }) {
    if (!editing) return;
    try {
      await patchTransaction.mutateAsync({
        id: editing.id,
        body: {
          category_key: patch.categoryKey,
          subcategory_key: patch.subcategoryKey,
          kind: patch.kind,
          notes: patch.notes,
          // Off unless the popover's "Also create a rule" box was ticked: an edit is one-time.
          create_rule: patch.createRule ?? false,
        },
      });
      setEditing(null);
    } catch (error) {
      toast({
        title: "Could not save transaction",
        description: error instanceof Error ? error.message : undefined,
        variant: "destructive",
      });
    }
  }

  function handleApplySuggestion(candidate: SuggestionCandidate) {
    if (!editing) return;
    applySuggestion.mutate({
      transactionId: editing.id,
      candidate,
      label: categoryLabel(categories, candidate.category_key, candidate.subcategory_key),
    });
    setEditing(null);
  }

  function handleRevertEditing() {
    if (!editing) return;
    revertToAutomatic.mutate({ transactionIds: [editing.id] });
    setEditing(null);
  }

  async function handleBulkRevert() {
    try {
      await revertToAutomatic.mutateAsync({ transactionIds: Array.from(selectedIds) });
      setSelectedIds(new Set());
      setAllMatchingSelected(false);
    } catch {
      // The hook already told the user; keep the selection so they can retry.
    }
  }

  async function handleExport(format: "csv" | "json") {
    setExporting(true);
    try {
      await downloadTransactionsExport(format, filters, gridParams);
    } catch (error) {
      toast({
        title: "Export failed",
        description: error instanceof Error ? error.message : undefined,
        variant: "destructive",
      });
    } finally {
      setExporting(false);
    }
  }

  return (
    <Page>
      <PageHeader
        title="Transactions"
        description={
          grid.isLoading || sel.isLoading ? "Loading…" : `${total.toLocaleString()} transaction${total === 1 ? "" : "s"}`
        }
        actions={
          <>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={exporting}
              title="Exports the transactions matching the current filters and user view"
              onClick={() => void handleExport("csv")}
            >
              Export CSV
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={exporting}
              title="Exports the transactions matching the current filters and user view"
              onClick={() => void handleExport("json")}
            >
              Export JSON
            </Button>
          </>
        }
      />

      <FiltersBar
        filters={filters}
        categories={categories}
        onChange={setFilters}
        grid={gridParams}
        onGridChange={setGridParams}
        accounts={accounts}
      />

      <BulkBar
        selectedCount={selectedIds.size}
        totalMatching={total}
        loadedCount={rows.length}
        categories={categories}
        onSelectAllMatching={() => void handleSelectAllMatching()}
        onClear={() => {
          setSelectedIds(new Set());
          setAllMatchingSelected(false);
        }}
        onApply={(patch) => void handleBulkApply(patch)}
        applying={bulkUpdate.isPending}
        onRevert={() => void handleBulkRevert()}
        reverting={revertToAutomatic.isPending}
        selectingAll={allMatchingIds.isPending}
        allMatchingSelected={allMatchingSelected}
      />

      {grid.isLoading || sel.isLoading ? (
        <LoadingState rows={8} />
      ) : grid.isError ? (
        <ErrorState
          description={grid.error instanceof Error ? grid.error.message : undefined}
          onRetry={() => void grid.refetch()}
        />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No transactions match these filters"
          description="Try widening the date range or clearing a filter."
        />
      ) : (
        <>
          <div ref={gridFocusRef} tabIndex={-1} className="outline-none" data-testid="transactions-grid">
          <TransactionsTable
            rows={rows}
            context={columnsContext}
            onRowClick={setEditing}
            totalCount={total}
            sort={gridParams.sort}
            onSortChange={(sort) => setGridParams({ sort })}
            onNearEnd={() => {
              if (grid.hasNextPage && !grid.isFetchingNextPage) void grid.fetchNextPage();
            }}
          />
          </div>
          {grid.isFetchingNextPage ? (
            <p className="text-center text-xs text-muted-foreground">Loading more…</p>
          ) : grid.hasNextPage ? (
            <div className="flex justify-center">
              <Button type="button" variant="outline" size="sm" onClick={() => void grid.fetchNextPage()}>
                Load more ({rows.length} of {total})
              </Button>
            </div>
          ) : null}
        </>
      )}

      {editing ? (
        <EditPopover
          transaction={editing}
          categories={categories}
          onSave={(patch) => void handleSaveEdit(patch)}
          onClose={() => setEditing(null)}
          saving={
            patchTransaction.isPending || applySuggestion.isPending || revertToAutomatic.isPending
          }
          candidates={candidates.data?.candidates}
          conflict={candidates.data?.conflict}
          onApplySuggestion={handleApplySuggestion}
          onRevert={handleRevertEditing}
        />
      ) : null}
    </Page>
  );
}

export const screenMeta = { path: "/spending/transactions", title: "Transactions" };
