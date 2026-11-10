import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Category, Transaction } from "./types";
import { TransactionsTable } from "./table";

const CATEGORIES: Category[] = [];

const TRANSACTIONS: Transaction[] = [
  {
    id: 1,
    statement_id: 1,
    account_id: 1,
    posted_date: "2026-02-05",
    transaction_date: null,
    description_raw: "GARDEN CENTER",
    description_clean: "GARDEN CENTER",
    amount_minor: 1820,
    currency: "USD",
    kind: "purchase",
    category_key: "others",
    subcategory_key: "uncategorized",
    merchant_key: "garden center",
    notes: null,
    needs_review: true,
  },
];

function noop() {
  /* no-op */
}

/**
 * Regression coverage for a real layout bug: `column.getSize()` always returns a number
 * (`@tanstack/react-table`'s own default, 150, when a column def sets none), so checking it —
 * as this table used to — can never distinguish "explicitly sized" from "should flex", and
 * every column ends up fixed-width: Description got clipped to a arbitrary 150px while unused
 * space sat blank past Notes. Only Description (the one column with no `size` in `columns.tsx`)
 * should get the flexible `1 1 0%`; every other column keeps the fixed width it asks for.
 */
describe("TransactionsTable column layout", () => {
  it("gives Description the flexible width and every other column a fixed one", () => {
    render(
      <TransactionsTable
        rows={TRANSACTIONS}
        context={{
          categories: CATEGORIES,
          selectedIds: new Set(),
          onToggleRow: noop,
          onEdit: noop,
        }}
      />,
    );

    const dateHeader = screen.getByText("Date");
    const descriptionHeader = screen.getByText("Description");
    const notesHeader = screen.getByText("Notes");

    expect(dateHeader.style.flex).toBe("0 0 104px");
    expect(notesHeader.style.flex).toBe("0 0 140px");
    expect(descriptionHeader.style.flex).toBe("1 1 0%");
  });

  it("keeps the date cell on one line and truncates a long description with a tooltip", () => {
    render(
      <TransactionsTable
        rows={TRANSACTIONS}
        context={{
          categories: CATEGORIES,
          selectedIds: new Set(),
          onToggleRow: noop,
          onEdit: noop,
        }}
      />,
    );

    const dateCell = screen.getByText("2026-02-05");
    expect(dateCell.className).toContain("whitespace-nowrap");

    const descriptionCell = screen.getByTitle("GARDEN CENTER");
    expect(descriptionCell.className).toContain("truncate");
  });

  it("makes the date and amount headers sort controls that flip direction and show it", async () => {
    const onSortChange = vi.fn();
    const context = { categories: CATEGORIES, selectedIds: new Set<number>(), onToggleRow: noop, onEdit: noop };
    const { rerender } = render(
      <TransactionsTable rows={TRANSACTIONS} context={context} sort="-posted_date" onSortChange={onSortChange} />,
    );

    const dateHeader = screen.getByRole("columnheader", { name: /Date/ });
    expect(dateHeader).toHaveAttribute("aria-sort", "descending");
    expect(screen.getByRole("columnheader", { name: /Amount/ })).toHaveAttribute("aria-sort", "none");
    // Description is not sortable: no `sort` API for it.
    expect(screen.getByRole("columnheader", { name: "Description" })).not.toHaveAttribute("aria-sort");

    await userEvent.click(dateHeader);
    expect(onSortChange).toHaveBeenLastCalledWith("posted_date");
    await userEvent.click(screen.getByRole("columnheader", { name: /Amount/ }));
    expect(onSortChange).toHaveBeenLastCalledWith("-amount_minor");

    rerender(
      <TransactionsTable rows={TRANSACTIONS} context={context} sort="-amount_minor" onSortChange={onSortChange} />,
    );
    await userEvent.click(screen.getByRole("columnheader", { name: /Amount/ }));
    expect(onSortChange).toHaveBeenLastCalledWith("amount_minor");
  });

  it("reports the query's total as aria-rowcount, not just the loaded rows", () => {
    render(
      <TransactionsTable
        rows={TRANSACTIONS}
        totalCount={1234}
        context={{ categories: CATEGORIES, selectedIds: new Set(), onToggleRow: noop, onEdit: vi.fn() }}
      />,
    );
    expect(screen.getByRole("table", { name: "Transactions" })).toHaveAttribute("aria-rowcount", "1235");
  });

  it("is one table with header and cell roles, and opens a row from its Edit button via the keyboard", async () => {
    const onEdit = vi.fn();
    const onRowClick = vi.fn();
    render(
      <TransactionsTable
        rows={TRANSACTIONS}
        onRowClick={onRowClick}
        context={{ categories: CATEGORIES, selectedIds: new Set(), onToggleRow: noop, onEdit }}
      />,
    );

    const table = screen.getByRole("table", { name: "Transactions" });
    // The header row is inside the table, followed by one body row.
    expect(within(table).getAllByRole("columnheader")).toHaveLength(7);
    expect(within(table).getAllByRole("row")).toHaveLength(2);
    expect(within(table).getAllByRole("cell")).toHaveLength(7);
    expect(table).toHaveAttribute("aria-rowcount", "2");

    const edit = screen.getByRole("button", { name: "Edit GARDEN CENTER" });
    edit.focus();
    await userEvent.keyboard("{Enter}");
    expect(onEdit).toHaveBeenCalledWith(TRANSACTIONS[0]);
    // One open per activation: the button's click does not also fire the row's handler.
    expect(onRowClick).not.toHaveBeenCalled();
    await userEvent.keyboard(" ");
    expect(onEdit).toHaveBeenCalledTimes(2);
  });
});
