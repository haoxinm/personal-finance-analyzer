import { getCoreRowModel, useReactTable, flexRender, type Column } from "@tanstack/react-table";
import * as React from "react";

import { cn } from "@/utils";

import { createColumns, type ColumnsContext } from "./columns";
import { nextSort } from "./use-grid-params";
import type { Transaction } from "./types";
import { useVirtualRows } from "./use-virtual-rows";

const ROW_HEIGHT = 44;
/** The grid's fixed viewport height (px). A fixed height is what makes windowing possible
 *  without measuring layout on every render. */
const VIEWPORT_HEIGHT = 560;

/**
 * `column.getSize()` always returns a number — `@tanstack/react-table` merges its own
 * `defaultColumn.size` (150) into *every* column's `columnDef` once the table processes it, even
 * one whose own def never set a `size` — so checking either `getSize()` or `columnDef.size` (as
 * this table used to) can never tell "explicitly sized" apart from "let it flex", and every
 * column ends up fixed-width: Description got clipped to that default 150px while unused space
 * sat blank past Notes. `columns.tsx` instead marks the one column that should absorb whatever
 * width the fixed-size columns leave behind with `meta: { flexible: true }` — `meta` is never
 * touched by `defaultColumn`, so it is the one place immune to this default-merging trap.
 */
function columnFlex(column: Column<Transaction, unknown>): string {
  return column.columnDef.meta?.flexible ? "1 1 0%" : `0 0 ${column.getSize()}px`;
}

export interface TransactionsTableProps {
  rows: Transaction[];
  context: ColumnsContext;
  onRowClick?: (transaction: Transaction) => void;
  /** Called when the user scrolls within `overscan` rows of the bottom, to page in more data
   *  (the caller is the one that knows whether there is a next page to fetch). */
  onNearEnd?: () => void;
  /** The active `sort` (`-posted_date`, `amount_minor`, ...) and the setter the sortable column
   *  headers (`meta.sortKey`) call with the next one. Without them the headers are plain text. */
  sort?: string;
  onSortChange?: (sort: string) => void;
}

/**
 * The transactions grid itself: a `@tanstack/react-table` column/row model (already a
 * dependency — no table logic is hand-rolled) rendered through the local windowing hook, so
 * only the rows within the visible range are ever mounted. `columns` is memoized on the
 * pieces of `context` that actually change (selection and the taxonomy list), never rebuilt
 * on every keystroke elsewhere on the page.
 */
export function TransactionsTable({
  rows,
  context,
  onRowClick,
  onNearEnd,
  sort,
  onSortChange,
}: TransactionsTableProps) {
  const columns = React.useMemo(
    () => createColumns(context),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- context is a plain object; depend on its fields, not its identity.
    [context.categories, context.selectedIds, context.onToggleRow, context.onEdit],
  );

  const table = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (row) => String(row.id),
  });

  const tableRows = table.getRowModel().rows;
  const { containerRef, range, onScroll } = useVirtualRows({
    rowCount: tableRows.length,
    rowHeight: ROW_HEIGHT,
    overscan: 10,
  });

  const handleScroll = React.useCallback(
    (e: React.UIEvent<HTMLDivElement>) => {
      onScroll();
      const el = e.currentTarget;
      if (onNearEnd && el.scrollHeight - el.scrollTop - el.clientHeight < ROW_HEIGHT * 20) {
        onNearEnd();
      }
    },
    [onScroll, onNearEnd],
  );

  const visibleRows = tableRows.slice(range.startIndex, range.endIndex);

  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <div className="flex border-b border-border bg-muted/50 text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {table.getFlatHeaders().map((header) => {
          const sortKey = header.column.columnDef.meta?.sortKey;
          const sortable = sortKey !== undefined && sort !== undefined && onSortChange !== undefined;
          const direction =
            sortKey === undefined || sort === undefined
              ? undefined
              : sort === sortKey
                ? "ascending"
                : sort === `-${sortKey}`
                  ? "descending"
                  : undefined;
          return (
            <div
              key={header.id}
              role="columnheader"
              aria-sort={sortable ? (direction ?? "none") : undefined}
              tabIndex={sortable ? 0 : undefined}
              className={cn(
                "flex items-center gap-1 overflow-hidden whitespace-nowrap px-3 py-2",
                sortable && "cursor-pointer select-none hover:text-foreground",
              )}
              style={{ flex: columnFlex(header.column) }}
              onClick={sortable ? () => onSortChange(nextSort(sort, sortKey)) : undefined}
              onKeyDown={
                sortable
                  ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onSortChange(nextSort(sort, sortKey));
                      }
                    }
                  : undefined
              }
            >
              {flexRender(header.column.columnDef.header, header.getContext())}
              {direction ? <span aria-hidden="true">{direction === "ascending" ? "▲" : "▼"}</span> : null}
            </div>
          );
        })}
      </div>
      <div
        ref={containerRef}
        onScroll={handleScroll}
        role="table"
        aria-rowcount={tableRows.length}
        className="overflow-auto"
        style={{ height: VIEWPORT_HEIGHT }}
        data-testid="transactions-viewport"
      >
        <div style={{ paddingTop: range.paddingTop, paddingBottom: range.paddingBottom }}>
          {visibleRows.map((row) => (
            <div
              key={row.id}
              role="row"
              className={cn(
                "flex cursor-pointer items-center border-b border-border/60 text-sm hover:bg-muted/40",
                context.selectedIds.has(row.original.id) && "bg-primary/5",
              )}
              style={{ height: ROW_HEIGHT }}
              onClick={() => onRowClick?.(row.original)}
            >
              {row.getVisibleCells().map((cell) => (
                <div
                  key={cell.id}
                  className="flex items-center overflow-hidden px-3"
                  style={{ flex: columnFlex(cell.column) }}
                >
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
