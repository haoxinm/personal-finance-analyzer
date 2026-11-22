import { fireEvent, render, screen, within } from "@testing-library/react";
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
      is_automatic: true,
    },
    {
      source: "builtin_rule" as const,
      category_key: "fuel",
      subcategory_key: "gasoline",
      kind: null,
      label: "ACME",
      rule_id: 2,
      is_current: false,
      is_automatic: false,
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
    // The applied answer is shown but is not a control: a click would only freeze it.
    expect(within(dialog).getByTestId("current-chip")).toHaveTextContent(
      "Groceries › Supermarket · your rule 'acme'",
    );
    expect(within(dialog).queryByRole("button", { name: /Supermarket/ })).not.toBeInTheDocument();

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

  it("offers Revert to automatic on an edited row, and none on an automatic one", async () => {
    const onRevert = vi.fn();
    const edited = [
      {
        source: "one_time_edit" as const,
        category_key: "fuel",
        subcategory_key: "gasoline",
        kind: null,
        label: "your one-time edit",
        rule_id: null,
        is_current: true,
        is_automatic: false,
      },
      { ...CANDIDATES[0]!, is_current: false, is_automatic: true },
    ];
    const { rerender } = render(
      <EditPopover
        transaction={makeTransaction()}
        categories={CATS}
        onSave={vi.fn()}
        onClose={vi.fn()}
        candidates={edited}
        conflict={false}
        onApplySuggestion={vi.fn()}
        onRevert={onRevert}
      />,
    );

    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByRole("button", { name: /^Automatic: Groceries › Supermarket/ }),
    ).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Revert to automatic" }));
    expect(onRevert).toHaveBeenCalledTimes(1);

    rerender(
      <EditPopover
        transaction={makeTransaction()}
        categories={CATS}
        onSave={vi.fn()}
        onClose={vi.fn()}
        candidates={CANDIDATES}
        conflict
        onApplySuggestion={vi.fn()}
        onRevert={onRevert}
      />,
    );
    expect(screen.queryByRole("button", { name: "Revert to automatic" })).not.toBeInTheDocument();
  });
});

describe("EditPopover details (corrections)", () => {
  const amount = () => screen.getByRole("textbox", { name: /^Amount/ });

  it("shows a summary of the transaction being edited", () => {
    setup(makeTransaction({ description_raw: "ACME RAW 123", description_clean: "Acme" }));
    const summary = screen.getByTestId("edit-summary");
    expect(summary).toHaveTextContent("2026-01-05");
    expect(summary).toHaveTextContent("Acme");
    expect(summary).toHaveTextContent("As printed: ACME RAW 123");
  });

  it("prefills date, description and the amount magnitude with its direction", () => {
    setup(makeTransaction());
    expect(screen.getByLabelText("Date")).toHaveValue("2026-01-05");
    expect(screen.getByLabelText("Description")).toHaveValue("ACME DEPOSIT");
    expect(amount()).toHaveValue("2500.00");
    expect(screen.getByRole("radio", { name: "Money in" })).toBeChecked();
  });

  it("sends only changed detail fields", async () => {
    const { onSave, save } = setup(makeTransaction({ amount_minor: 1000 }));
    await userEvent.clear(screen.getByLabelText("Description"));
    await userEvent.type(screen.getByLabelText("Description"), "  Better name ");
    fireEvent.change(screen.getByLabelText("Date"), { target: { value: "2026-01-06" } });
    await userEvent.click(save());
    expect(onSave).toHaveBeenCalledWith({
      postedDate: "2026-01-06",
      descriptionClean: "Better name",
    });
  });

  it("signs the amount from the direction: out is positive, in is negative", async () => {
    const { onSave, save } = setup(makeTransaction({ amount_minor: 1000 }));
    await userEvent.clear(amount());
    await userEvent.type(amount(), "12.5");
    await userEvent.click(save());
    expect(onSave).toHaveBeenLastCalledWith({ amountMinor: 1250 });

    await userEvent.click(screen.getByRole("radio", { name: "Money in" }));
    await userEvent.click(save());
    expect(onSave).toHaveBeenLastCalledWith({ amountMinor: -1250 });
  });

  it("flipping only the direction changes the sign", async () => {
    const { onSave, save } = setup(makeTransaction({ amount_minor: 1000 }));
    await userEvent.click(screen.getByRole("radio", { name: "Money in" }));
    await userEvent.click(save());
    expect(onSave).toHaveBeenCalledWith({ amountMinor: -1000 });
  });

  it("disables Save with an inline error for an invalid amount, then recovers", async () => {
    const { save } = setup(makeTransaction({ notes: "n" }));
    await userEvent.type(screen.getByLabelText("Notes"), "x");
    expect(save()).toBeEnabled();
    await userEvent.clear(amount());
    await userEvent.type(amount(), "1.234");
    expect(save()).toBeDisabled();
    expect(amount()).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("alert")).toHaveTextContent(/at most 2 decimals/);
    await userEvent.clear(amount());
    await userEvent.type(amount(), "1.23");
    expect(save()).toBeEnabled();
  });

  it("disables Save for an empty description or date", () => {
    const { save } = setup(makeTransaction());
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "   " } });
    expect(save()).toBeDisabled();
    expect(screen.getByLabelText("Description")).toHaveAttribute("aria-invalid", "true");
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "ok" } });
    expect(save()).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Date"), { target: { value: "" } });
    expect(save()).toBeDisabled();
  });

  it("offers Imported as / Restore only for corrected fields", () => {
    setup(
      makeTransaction({
        description_clean: "Fixed",
        corrections: { description_clean: "FIXD 99", amount_minor: null },
      }),
    );
    expect(screen.getByRole("button", { name: "Restore imported description" })).toBeInTheDocument();
    expect(screen.getByText("FIXD 99")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restore imported date" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restore imported amount" })).not.toBeInTheDocument();
  });

  it("restoring then saving sends revertFields instead of a value", async () => {
    const { onSave, save } = setup(
      makeTransaction({
        description_clean: "Fixed",
        amount_minor: 2000,
        posted_date: "2026-01-09",
        corrections: { description_clean: "FIXD 99", amount_minor: -2000, posted_date: "2026-01-05" },
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Restore imported description" }));
    await userEvent.click(screen.getByRole("button", { name: "Restore imported amount" }));
    await userEvent.click(screen.getByRole("button", { name: "Restore imported date" }));
    expect(screen.getByLabelText("Description")).toHaveValue("FIXD 99");
    expect(amount()).toHaveValue("20.00");
    expect(screen.getByRole("radio", { name: "Money in" })).toBeChecked();
    await userEvent.click(save());
    expect(onSave).toHaveBeenCalledWith({
      revertFields: ["posted_date", "description_clean", "amount_minor"],
    });
  });

  it("wraps long unbroken text instead of widening the dialog", () => {
    const long = "A".repeat(400);
    setup(makeTransaction({ description_clean: long, description_raw: long + "RAW", merchant_key: long }));
    const dialog = screen.getByRole("dialog");
    const body = dialog.querySelector("[data-dialog-body]");
    expect(body?.className).toContain("[overflow-wrap:anywhere]");
    expect(body?.className).toContain("overflow-y-auto");
    expect(screen.getByText(/As printed:/).className).toContain("break-all");
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });
});

describe("EditPopover unsaved changes", () => {
  function setupClose(transaction: Transaction) {
    const onClose = vi.fn();
    render(<EditPopover transaction={transaction} categories={CATEGORIES} onSave={vi.fn()} onClose={onClose} />);
    return { onClose };
  }

  it("is named by its visible title", () => {
    setupClose(makeTransaction());
    const dialog = screen.getByRole("dialog", { name: "Edit transaction" });
    expect(dialog).toHaveAttribute("aria-labelledby", screen.getByRole("heading", { name: "Edit transaction" }).id);
  });

  it("closes straight away when nothing changed", async () => {
    const { onClose } = setupClose(makeTransaction());
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("asks before discarding edits on Escape, Cancel and an outside press", async () => {
    const { onClose } = setupClose(makeTransaction());
    await userEvent.type(screen.getByLabelText("Notes"), "x");
    await userEvent.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByText("Discard your unsaved changes?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Keep editing" })).toHaveFocus();
    // Escape again backs out of the question.
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByText("Discard your unsaved changes?")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByText("Discard your unsaved changes?")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(onClose).not.toHaveBeenCalled();
    await userEvent.click(screen.getByTestId("dialog-overlay"));
    expect(screen.getByText("Discard your unsaved changes?")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("ignores Escape and an outside press while saving", async () => {
    const onClose = vi.fn();
    render(
      <EditPopover transaction={makeTransaction()} categories={CATEGORIES} onSave={vi.fn()} onClose={onClose} saving />,
    );
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByTestId("dialog-overlay"));
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("shows a save error inline in the footer", () => {
    render(
      <EditPopover
        transaction={makeTransaction()}
        categories={CATEGORIES}
        onSave={vi.fn()}
        onClose={vi.fn()}
        saveError="Could not save: boom"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Could not save: boom");
  });
});

describe("EditPopover details", () => {
  it("limits the date to 1970 .. a year from now", () => {
    setup(makeTransaction());
    const date = screen.getByLabelText<HTMLInputElement>("Date");
    expect(date.min).toBe("1970-01-01");
    const max = new Date();
    max.setDate(max.getDate() + 366);
    expect(date.max).toBe(
      `${max.getFullYear()}-${String(max.getMonth() + 1).padStart(2, "0")}-${String(max.getDate()).padStart(2, "0")}`,
    );
  });

  it("hints at a refund when a purchase is switched to Money in, and applies it on request", async () => {
    const { save } = setup(makeTransaction({ kind: "purchase", amount_minor: 5000 }));
    expect(screen.queryByTestId("refund-hint")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: "Money in" }));
    expect(screen.getByTestId("refund-hint")).toBeInTheDocument();
    expect(screen.getByLabelText<HTMLSelectElement>("Kind").value).toBe("purchase");
    await userEvent.click(screen.getByRole("button", { name: "Set kind to refund" }));
    expect(screen.getByLabelText<HTMLSelectElement>("Kind").value).toBe("refund");
    expect(screen.queryByTestId("refund-hint")).not.toBeInTheDocument();
    expect(save()).toBeEnabled();
  });
});
