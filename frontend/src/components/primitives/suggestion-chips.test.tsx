import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SuggestionChips } from "./suggestion-chips";
import { categoryLabel, sourceLabel, type SuggestionCandidate } from "./suggestion-labels";

const CATEGORIES = [
  {
    key: "restaurant",
    name: "Restaurant",
    subcategories: [{ key: "dine_in", name: "Dine in" }],
  },
  {
    key: "grocery",
    name: "Grocery",
    subcategories: [{ key: "wholesale_clubs", name: "Wholesale clubs" }],
  },
  { key: "fuel", name: "Fuel", subcategories: [{ key: "gasoline", name: "Gasoline" }] },
];

function candidate(overrides: Partial<SuggestionCandidate>): SuggestionCandidate {
  return {
    source: "user_rule",
    category_key: "restaurant",
    subcategory_key: "dine_in",
    kind: null,
    label: "costco",
    rule_id: 3,
    is_current: false,
    is_automatic: false,
    ...overrides,
  };
}

const CONFLICT: SuggestionCandidate[] = [
  candidate({ is_current: true, is_automatic: true }),
  candidate({
    source: "builtin_rule",
    category_key: "grocery",
    subcategory_key: "wholesale_clubs",
    label: "COSTCO",
    rule_id: 9,
  }),
  candidate({
    source: "llm",
    category_key: "fuel",
    subcategory_key: "gasoline",
    label: "learned from LLM",
    rule_id: null,
  }),
];

const EDITED: SuggestionCandidate[] = [
  candidate({
    source: "one_time_edit",
    category_key: "fuel",
    subcategory_key: "gasoline",
    label: "your one-time edit",
    rule_id: null,
    is_current: true,
  }),
  candidate({ is_automatic: true }),
  candidate({
    source: "builtin_rule",
    category_key: "grocery",
    subcategory_key: "wholesale_clubs",
    label: "COSTCO",
    rule_id: 9,
  }),
];

describe("SuggestionChips", () => {
  it("renders one chip per candidate, worded 'Category › Subcategory · source'", () => {
    render(
      <SuggestionChips candidates={CONFLICT} conflict categories={CATEGORIES} onApply={vi.fn()} />,
    );

    // The applied answer is a plain chip, the other two are buttons.
    expect(screen.getByTestId("current-chip")).toHaveTextContent(
      "Restaurant › Dine in · your rule 'costco'",
    );
    const chips = screen.getAllByRole("button");
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveTextContent("Grocery › Wholesale clubs · built-in");
    expect(chips[1]).toHaveTextContent("Fuel › Gasoline · LLM (saved)");
  });

  it("shows the automatic winner on a non-edited row as an inert chip, not a button", async () => {
    const onApply = vi.fn();
    const user = userEvent.setup();
    render(
      <SuggestionChips
        candidates={CONFLICT}
        conflict
        categories={CATEGORIES}
        onApply={onApply}
      />,
    );

    const current = screen.getByTestId("current-chip");
    expect(current).toHaveTextContent("Current");
    expect(current.tagName).not.toBe("BUTTON");
    expect(current).toHaveAttribute("title", expect.stringContaining("automatic answer"));
    expect(
      screen.queryByRole("button", { name: /Restaurant › Dine in/ }),
    ).not.toBeInTheDocument();
    // Clicking it does nothing: it would only freeze the row as a one-time edit.
    await user.click(current);
    expect(onApply).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Revert to automatic" })).not.toBeInTheDocument();
  });

  it("explains a conflict and highlights the group; says something milder without one", () => {
    const { rerender } = render(
      <SuggestionChips candidates={CONFLICT} conflict categories={CATEGORIES} onApply={vi.fn()} />,
    );
    expect(screen.getByRole("note")).toHaveTextContent(
      "Sources disagree — the highest-priority one is applied; click another to use it for this transaction only.",
    );
    expect(screen.getByTestId("suggestion-chips")).toHaveAttribute("data-conflict", "true");

    rerender(
      <SuggestionChips
        candidates={CONFLICT.slice(0, 2).map((c) => ({ ...c, category_key: "grocery" }))}
        conflict={false}
        categories={CATEGORIES}
        onApply={vi.fn()}
      />,
    );
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    expect(screen.getByTestId("suggestion-chips")).toHaveAttribute("data-conflict", "false");
  });

  it("applies the clicked candidate (and only it), by mouse or keyboard", async () => {
    const onApply = vi.fn();
    const user = userEvent.setup();
    render(
      <SuggestionChips
        candidates={CONFLICT}
        conflict
        categories={CATEGORIES}
        onApply={onApply}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Grocery › Wholesale clubs, built-in" }));
    expect(onApply).toHaveBeenCalledTimes(1);
    expect(onApply).toHaveBeenLastCalledWith(CONFLICT[1]);

    await user.tab(); // focus moves chip to chip in DOM order
    await user.keyboard("{Enter}");
    expect(onApply).toHaveBeenCalledTimes(2);
    expect(onApply.mock.calls[1]![0]).toEqual(CONFLICT[2]);
  });

  it("never offers the one-time edit already in place as something to apply", async () => {
    const onApply = vi.fn();
    const user = userEvent.setup();
    render(
      <SuggestionChips
        candidates={EDITED}
        conflict={false}
        categories={CATEGORIES}
        onApply={onApply}
      />,
    );

    const edit = screen.getByTestId("current-chip");
    expect(edit).toHaveTextContent("Fuel › Gasoline · your edit");
    expect(edit.tagName).not.toBe("BUTTON");
    await user.click(edit);
    expect(onApply).not.toHaveBeenCalled();
  });

  it("labels the automatic answer on an edited row and offers Revert to automatic", async () => {
    const onApply = vi.fn();
    const onRevert = vi.fn();
    const user = userEvent.setup();
    render(
      <SuggestionChips
        candidates={EDITED}
        conflict={false}
        categories={CATEGORIES}
        onApply={onApply}
        onRevert={onRevert}
      />,
    );

    expect(screen.getByTestId("suggestion-edited-note")).toHaveTextContent("revert to automatic");
    const automatic = screen.getByRole("button", {
      name: "Automatic: Restaurant › Dine in, your rule 'costco', reverts to automatic",
    });
    expect(automatic).toHaveTextContent("Automatic");

    // The explicit button and the Automatic chip both hand the row back; neither is an edit.
    await user.click(screen.getByRole("button", { name: "Revert to automatic" }));
    await user.click(automatic);
    expect(onRevert).toHaveBeenCalledTimes(2);
    expect(onApply).not.toHaveBeenCalled();

    // A lower layer is still an ordinary one-time edit.
    await user.click(screen.getByRole("button", { name: "Grocery › Wholesale clubs, built-in" }));
    expect(onApply).toHaveBeenCalledWith(EDITED[2]);
  });

  it("offers no revert without a handler or on a row that was not edited", () => {
    const { rerender } = render(
      <SuggestionChips
        candidates={EDITED}
        conflict={false}
        categories={CATEGORIES}
        onApply={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Revert to automatic" })).not.toBeInTheDocument();

    rerender(
      <SuggestionChips
        candidates={CONFLICT}
        conflict
        categories={CATEGORIES}
        onApply={vi.fn()}
        onRevert={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Revert to automatic" })).not.toBeInTheDocument();
  });

  it("still offers Revert on an edited row whose rules have no answer, and says what happens", async () => {
    const onRevert = vi.fn();
    render(
      <SuggestionChips
        candidates={EDITED.slice(0, 1)}
        conflict={false}
        categories={CATEGORIES}
        onApply={vi.fn()}
        onRevert={onRevert}
      />,
    );

    expect(screen.getByTestId("revert-fallback-note")).toHaveTextContent(
      "Uncategorized and be flagged for review",
    );
    await userEvent.click(screen.getByRole("button", { name: "Revert to automatic" }));
    expect(onRevert).toHaveBeenCalledTimes(1);
  });

  it("disables every chip while busy", () => {
    const onApply = vi.fn();
    render(
      <SuggestionChips
        candidates={CONFLICT}
        conflict
        categories={CATEGORIES}
        onApply={onApply}
        disabled
      />,
    );
    for (const chip of screen.getAllByRole("button")) expect(chip).toBeDisabled();
  });

  it("renders nothing when there is nothing to choose between", () => {
    const { container, rerender } = render(
      <SuggestionChips candidates={[]} conflict={false} categories={CATEGORIES} onApply={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();

    rerender(
      <SuggestionChips
        candidates={[candidate({ is_current: true })]}
        conflict={false}
        categories={CATEGORIES}
        onApply={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("falls back to the raw keys for a category the taxonomy does not list", () => {
    expect(categoryLabel(CATEGORIES, "mystery", "thing")).toBe("mystery › thing");
    expect(categoryLabel(CATEGORIES, "fuel", "nope")).toBe("Fuel › nope");
  });

  it("words every source", () => {
    const words = (source: SuggestionCandidate["source"]) =>
      sourceLabel(candidate({ source, label: "x" }));
    expect(words("user_rule")).toBe("your rule 'x'");
    expect(words("builtin_rule")).toBe("built-in");
    expect(words("issuer")).toBe("issuer");
    expect(words("merchant_cache")).toBe("merchant cache");
    expect(words("llm")).toBe("LLM (saved)");
    expect(words("one_time_edit")).toBe("your edit");
    expect(words("kind")).toBe("payments & transfers");
  });

  it("mentions a rule's kind override in the chip's tooltip", () => {
    render(
      <SuggestionChips
        candidates={[candidate({ is_current: true }), candidate({ source: "issuer", kind: "refund", label: "DINING" })]}
        conflict={false}
        categories={CATEGORIES}
        onApply={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Restaurant › Dine in, issuer" })).toHaveAttribute(
      "title",
      expect.stringContaining("sets kind to refund"),
    );
  });
});
