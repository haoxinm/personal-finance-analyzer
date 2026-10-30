import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { SpendQueryParams } from "@/hooks/use-spend-query-params";

import type { AccountOption } from "./api";
import { FiltersBar, type FiltersBarProps } from "./filters";
import type { Category } from "./types";
import { DEFAULT_SORT, type GridParams } from "./use-grid-params";

const CATEGORIES: Category[] = [
  { key: "groceries", name: "Groceries", subcategories: [] },
  { key: "dining", name: "Dining", subcategories: [] },
];

function baseFilters(): SpendQueryParams {
  return {};
}

const ACCOUNTS: AccountOption[] = [
  { id: 1, userId: 1, label: "Chase · credit" },
  { id: 2, userId: 1, label: "Chase · checking" },
];

/** `FiltersBar` with the grid-only props defaulted, so each test names only what it cares about. */
function Bar(
  props: Pick<FiltersBarProps, "filters" | "onChange"> &
    Partial<Pick<FiltersBarProps, "grid" | "onGridChange" | "accounts">>,
) {
  const grid: GridParams = { sort: DEFAULT_SORT };
  return (
    <FiltersBar
      categories={CATEGORIES}
      grid={grid}
      onGridChange={vi.fn()}
      accounts={ACCOUNTS}
      {...props}
    />
  );
}

describe("FiltersBar", () => {
  it("toggles a kind filter immediately (no debounce)", async () => {
    const onChange = vi.fn();
    render(<Bar filters={baseFilters()} onChange={onChange} />);

    await userEvent.click(screen.getByRole("button", { name: "purchase" }));

    expect(onChange).toHaveBeenCalledWith({ kinds: ["purchase"] });
  });

  it("toggles a category filter immediately", async () => {
    const onChange = vi.fn();
    render(<Bar filters={baseFilters()} onChange={onChange} />);

    await userEvent.click(screen.getByRole("button", { name: "Groceries" }));

    expect(onChange).toHaveBeenCalledWith({ categoryKeys: ["groceries"] });
  });

  it("un-toggles a kind that is already selected", async () => {
    const onChange = vi.fn();
    render(
      <Bar filters={{ kinds: ["purchase"] }} onChange={onChange} />,
    );

    await userEvent.click(screen.getByRole("button", { name: "purchase" }));

    expect(onChange).toHaveBeenCalledWith({ kinds: undefined });
  });

  it("debounces the search box", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onChange = vi.fn();
    render(<Bar filters={baseFilters()} onChange={onChange} />);

    await userEvent.type(screen.getByLabelText("Search"), "coffee", {
      advanceTimers: (ms) => vi.advanceTimersByTime(ms),
    });

    expect(onChange).not.toHaveBeenCalled();
    vi.advanceTimersByTime(300);
    expect(onChange).toHaveBeenCalledWith({ search: "coffee" });
    vi.useRealTimers();
  });

  it("shows a Clear filters button only once a filter is active", () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <Bar filters={baseFilters()} onChange={onChange} />,
    );
    expect(screen.queryByText("Clear filters")).not.toBeInTheDocument();

    rerender(
      <Bar filters={{ search: "coffee" }} onChange={onChange} />,
    );
    expect(screen.getByText("Clear filters")).toBeInTheDocument();
  });

  it("toggles the needs-review and uncategorized filters through the grid params", async () => {
    const onGridChange = vi.fn();
    render(<Bar filters={baseFilters()} onChange={vi.fn()} onGridChange={onGridChange} />);

    await userEvent.click(screen.getByRole("button", { name: "Needs review" }));
    expect(onGridChange).toHaveBeenLastCalledWith({ needsReview: true });
    await userEvent.click(screen.getByRole("button", { name: "Uncategorized only" }));
    expect(onGridChange).toHaveBeenLastCalledWith({ uncategorized: true });
  });

  it("un-toggles an active needs-review filter and lists it as an active filter", async () => {
    const onGridChange = vi.fn();
    render(
      <Bar
        filters={baseFilters()}
        onChange={vi.fn()}
        onGridChange={onGridChange}
        grid={{ sort: DEFAULT_SORT, needsReview: true }}
      />,
    );
    expect(screen.getByRole("button", { name: "Needs review" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Clear filters")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Needs review" }));
    expect(onGridChange).toHaveBeenLastCalledWith({ needsReview: undefined });
  });

  it("filters by account, and shows a removable chip for a single-statement view", async () => {
    const onChange = vi.fn();
    const onGridChange = vi.fn();
    render(
      <Bar
        filters={baseFilters()}
        onChange={onChange}
        onGridChange={onGridChange}
        grid={{ sort: DEFAULT_SORT, statementId: 9 }}
      />,
    );
    await userEvent.selectOptions(screen.getByLabelText("Account"), "2");
    expect(onChange).toHaveBeenCalledWith({ accountIds: [2] });
    await userEvent.selectOptions(screen.getByLabelText("Account"), "");
    expect(onChange).toHaveBeenLastCalledWith({ accountIds: undefined });

    await userEvent.click(screen.getByRole("button", { name: "Remove filter: statement 9" }));
    expect(onGridChange).toHaveBeenCalledWith({ statementId: undefined });
  });

  it("hides the account filter when there is only one account to choose", () => {
    render(<Bar filters={baseFilters()} onChange={vi.fn()} accounts={ACCOUNTS.slice(0, 1)} />);
    expect(screen.queryByLabelText("Account")).not.toBeInTheDocument();
  });
});
