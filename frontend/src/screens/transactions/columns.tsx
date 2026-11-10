import type { ColumnDef } from "@tanstack/react-table";

import { CategoryBadge } from "@/components/primitives/category-badge";
import { Money } from "@/components/primitives/money";

import type { Category, Transaction } from "./types";

declare module "@tanstack/react-table" {
  // Merging into `ColumnMeta` requires matching its own type parameter names exactly (TS2428),
  // so `TData`/`TValue` can't be renamed `_TData`/`_TValue` to quiet the unused-vars rule the
  // normal way; neither is actually used in this interface's body.
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData, TValue> {
    /** Marks the one column (Description) that should absorb whatever width the table's
     *  fixed-size columns leave behind, rather than getting a fixed width of its own
     *  (`table.tsx`'s `columnFlex` reads this — see its doc comment for why). */
    flexible?: boolean;
    /** Makes the column header a sort control: the `GET /transactions` `sort` key it toggles
     *  (`posted_date` or `amount_minor`; a `-` prefix on the active sort means descending). */
    sortKey?: "posted_date" | "amount_minor";
  }
}

export interface ColumnsContext {
  categories: Category[];
  selectedIds: Set<number>;
  onToggleRow: (id: number) => void;
  onEdit: (transaction: Transaction) => void;
}

function categoryLabel(categories: Category[], key: string | null): string | undefined {
  if (key === null) return undefined;
  return categories.find((c) => c.key === key)?.name;
}

function subcategoryLabel(
  categories: Category[],
  categoryKey: string | null,
  subcategoryKey: string | null,
): string | undefined {
  if (categoryKey === null || subcategoryKey === null) return undefined;
  const category = categories.find((c) => c.key === categoryKey);
  return category?.subcategories.find((s) => s.key === subcategoryKey)?.name;
}

/**
 * Builds the transactions grid's column defs. A factory, not a module-level constant, because
 * cell renderers close over `context` (selection state, the edit callback, the taxonomy) — but
 * every call site memoizes the result with `React.useMemo(() => createColumns(context), [...])`
 * keyed on `context`'s own pieces, so a column def is never rebuilt on an unrelated re-render
 * (§P3_BRIEF: "never re-create column defs per render").
 */
export function createColumns(context: ColumnsContext): ColumnDef<Transaction>[] {
  return [
    {
      id: "select",
      header: () => <span className="sr-only">Select</span>,
      size: 36,
      cell: ({ row }) => (
        <input
          type="checkbox"
          aria-label={`Select transaction ${row.original.id}`}
          checked={context.selectedIds.has(row.original.id)}
          onChange={() => context.onToggleRow(row.original.id)}
          onClick={(e) => e.stopPropagation()}
        />
      ),
    },
    {
      id: "date",
      header: "Date",
      accessorFn: (row) => row.posted_date,
      cell: ({ row }) => (
        <span className="whitespace-nowrap">{row.original.posted_date}</span>
      ),
      size: 104,
      meta: { sortKey: "posted_date" },
    },
    {
      id: "description",
      header: "Description",
      accessorFn: (row) => row.description_clean,
      // A real button so the keyboard can open the editor (the row's own click handler is
      // mouse-only); it looks like text and keeps the truncation + tooltip.
      cell: ({ row }) => (
        <button
          type="button"
          aria-label={`Edit ${row.original.description_clean}`}
          aria-haspopup="dialog"
          className="block w-full min-w-0 rounded-sm text-left hover:underline"
          onClick={(e) => {
            e.stopPropagation();
            context.onEdit(row.original);
          }}
        >
          <span className="block truncate" title={row.original.description_clean}>
            {row.original.description_clean}
          </span>
        </button>
      ),
      // `column.getSize()` (and even `column.columnDef.size` once `@tanstack/react-table` has
      // merged its own `defaultColumn.size` into every column) is never `undefined` — so
      // `meta.flexible` is the only reliable way to mark "the one column that should absorb
      // whatever width the fixed-size columns leave behind" (`table.tsx` reads this flag).
      meta: { flexible: true },
    },
    {
      id: "category",
      header: "Category",
      cell: ({ row }) => {
        const txn = row.original;
        if (txn.category_key === null) {
          return (
            <button
              type="button"
              className="whitespace-nowrap text-xs text-muted-foreground underline-offset-2 hover:underline"
              onClick={(e) => {
                e.stopPropagation();
                context.onEdit(txn);
              }}
            >
              Uncategorized
            </button>
          );
        }
        return (
          <button
            type="button"
            className="block w-full min-w-0 text-left"
            onClick={(e) => {
              e.stopPropagation();
              context.onEdit(txn);
            }}
          >
            <CategoryBadge
              categoryKey={txn.category_key}
              label={categoryLabel(context.categories, txn.category_key)}
              subcategoryLabel={subcategoryLabel(
                context.categories,
                txn.category_key,
                txn.subcategory_key,
              )}
              needsReview={txn.needs_review}
            />
          </button>
        );
      },
      size: 240,
    },
    {
      id: "kind",
      header: "Kind",
      accessorFn: (row) => row.kind,
      cell: ({ row }) => <span className="whitespace-nowrap">{row.original.kind}</span>,
      size: 90,
    },
    {
      id: "amount",
      header: "Amount",
      cell: ({ row }) => (
        <Money minorUnits={row.original.amount_minor} currency={row.original.currency} />
      ),
      size: 110,
      meta: { sortKey: "amount_minor" },
    },
    {
      id: "notes",
      header: "Notes",
      cell: ({ row }) => (
        <span className="block truncate text-muted-foreground" title={row.original.notes ?? undefined}>
          {row.original.notes}
        </span>
      ),
      size: 140,
    },
  ];
}
