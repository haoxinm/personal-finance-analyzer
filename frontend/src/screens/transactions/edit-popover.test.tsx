import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { EditPopover } from "./edit-popover";
import type { Category, Transaction } from "./types";

const CATEGORIES: Category[] = [
  {
    key: "groceries",
    name: "Groceries",
    subcategories: [{ id: 1, key: "supermarket", name: "Supermarket", pending: false }],
  },
];

function makeTransaction(overrides: Partial<Transaction> = {}): Transaction {
  return {
    id: 7,
    statement_id: 1,
    account_id: 1,
    posted_date: "2026-01-05",
    transaction_date: null,
    description_raw: "ACME DEPOSIT",
    description_clean: "ACME DEPOSIT",
    amount_minor: -250000,
    currency: "USD",
    kind: "refund",
    category_key: null,
    subcategory_key: null,
    merchant_key: "acme",
    notes: null,
    needs_review: false,
    ...overrides,
  };
}

function setup(transaction: Transaction) {
  const onSave = vi.fn();
  render(
    <EditPopover
      transaction={transaction}
      categories={CATEGORIES}
      onSave={onSave}
      onClose={vi.fn()}
    />,
  );
  return { onSave, save: () => screen.getByRole("button", { name: "Save" }) };
}

describe("EditPopover", () => {
  it("traps Tab and Shift+Tab inside the dialog", async () => {
    const { save } = setup(makeTransaction({ notes: "n" }));
    const dialog = screen.getByRole("dialog");
    const cancel = screen.getByRole("button", { name: "Cancel" });
    const category = screen.getByLabelText("Category");
    // Make Save enabled so it is the last focusable control.
    await userEvent.type(screen.getByLabelText("Notes"), "x");
    save().focus();
    await userEvent.tab();
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    expect(document.activeElement).toBe(category);
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(save());
    cancel.focus();
    await userEvent.tab();
    expect(document.activeElement).toBe(save());
  });

  it("disables Save until something changes", () => {
    const { save } = setup(makeTransaction());
    expect(save()).toBeDisabled();
  });

  it("saves a kind change on its own, without choosing a category", async () => {
    const { onSave, save } = setup(makeTransaction());
    await userEvent.selectOptions(screen.getByLabelText("Kind"), "transfer");
    expect(save()).toBeEnabled();
    await userEvent.click(save());
    expect(onSave).toHaveBeenCalledWith({ kind: "transfer" });
  });

  it("saves notes on their own, including clearing them", async () => {
    const { onSave, save } = setup(makeTransaction({ notes: "old note" }));
    const notes = screen.getByLabelText("Notes");
    expect(notes).toHaveValue("old note");
    await userEvent.clear(notes);
    await userEvent.click(save());
    expect(onSave).toHaveBeenCalledWith({ notes: "" });
  });

  it("requires both halves of a category change, then sends only the changed fields", async () => {
    const { onSave, save } = setup(makeTransaction());
    await userEvent.selectOptions(screen.getByLabelText("Category"), "groceries");
    // Category chosen but no subcategory yet: not a complete pair, so it cannot be saved.
    expect(save()).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("Subcategory"), "supermarket");
    await userEvent.click(save());
    expect(onSave).toHaveBeenCalledWith({ categoryKey: "groceries", subcategoryKey: "supermarket" });
  });
});

describe("EditPopover scope", () => {
  const merchantTxn = () => makeTransaction({ merchant_key: "acme deposit" });

  it("says the change applies to this transaction only", () => {
    setup(merchantTxn());
    expect(screen.getByTestId("edit-scope-note")).toHaveTextContent(
      "This change applies to this transaction only.",
    );
  });

  it("offers an unchecked rule checkbox naming the merchant, disabled until a category is chosen", async () => {
    setup(merchantTxn());
    const rule = screen.getByRole("checkbox", { name: /Also create a rule for “acme deposit”/ });
    expect(rule).not.toBeChecked();
    expect(rule).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("Category"), "groceries");
    expect(rule).toBeDisabled(); // still only half a category
    await userEvent.selectOptions(screen.getByLabelText("Subcategory"), "supermarket");
    expect(rule).toBeEnabled();
    expect(rule).not.toBeChecked();
  });

  it("saves a plain edit without createRule by default", async () => {
    const { onSave, save } = setup(merchantTxn());
    await userEvent.selectOptions(screen.getByLabelText("Category"), "groceries");
    await userEvent.selectOptions(screen.getByLabelText("Subcategory"), "supermarket");
    await userEvent.click(save());
    expect(onSave).toHaveBeenCalledWith({ categoryKey: "groceries", subcategoryKey: "supermarket" });
  });

  it("sends createRule only when the rule checkbox is ticked", async () => {
    const { onSave, save } = setup(merchantTxn());
    await userEvent.selectOptions(screen.getByLabelText("Category"), "groceries");
    await userEvent.selectOptions(screen.getByLabelText("Subcategory"), "supermarket");
    await userEvent.click(screen.getByRole("checkbox", { name: /Also create a rule/ }));
    await userEvent.click(save());
    expect(onSave).toHaveBeenCalledWith({
      categoryKey: "groceries",
      subcategoryKey: "supermarket",
      createRule: true,
    });
  });

  it("hides the rule checkbox for a transaction with no merchant to match", () => {
    setup(makeTransaction({ merchant_key: "" }));
    expect(screen.queryByRole("checkbox", { name: /Also create a rule/ })).not.toBeInTheDocument();
  });
});

describe("EditPopover suggestions", () => {
  const CATS: Category[] = [
    ...CATEGORIES,
    {
      key: "fuel",
      name: "Fuel",
      subcategories: [{ id: 2, key: "gasoline", name: "Gasoline", pending: false }],
    },
  ];
  const CANDIDATES = [
    {
      source: "user_rule" as const,
      category_key: "groceries",
      subcategory_key: "supermarket",
      kind: null,
      label: "acme",
      rule_id: 1,
      is_current: true,
    },
    {
      source: "builtin_rule" as const,
      category_key: "fuel",
      subcategory_key: "gasoline",
      kind: null,
      label: "ACME",
      rule_id: 2,
      is_current: false,
    },
  ];

  it("shows the chips above the form and applies the clicked candidate", async () => {
    const onApplySuggestion = vi.fn();
    const onSave = vi.fn();
    render(
      <EditPopover
        transaction={makeTransaction()}
        categories={CATS}
        onSave={onSave}
        onClose={vi.fn()}
        candidates={CANDIDATES}
        conflict
        onApplySuggestion={onApplySuggestion}
      />,
    );

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("note")).toHaveTextContent("Sources disagree");
    expect(
      within(dialog).getByRole("button", { name: "Groceries › Supermarket, your rule 'acme', current" }),
    ).toHaveAttribute("aria-pressed", "true");

    await userEvent.click(within(dialog).getByRole("button", { name: "Fuel › Gasoline, built-in" }));

    expect(onApplySuggestion).toHaveBeenCalledWith(CANDIDATES[1]);
    expect(onSave).not.toHaveBeenCalled(); // a chip is not the form's Save
  });

  it("renders no chips until candidates are given", () => {
    render(
      <EditPopover
        transaction={makeTransaction()}
        categories={CATS}
        onSave={vi.fn()}
        onClose={vi.fn()}
        onApplySuggestion={vi.fn()}
      />,
    );
    expect(screen.queryByRole("group", { name: "Suggested categories" })).not.toBeInTheDocument();
  });
});
