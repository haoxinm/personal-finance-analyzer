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
    ...overrides,
  };
}

const CONFLICT: SuggestionCandidate[] = [
  candidate({ is_current: true }),
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

describe("SuggestionChips", () => {
  it("renders one chip per candidate, worded 'Category › Subcategory · source'", () => {
    render(
      <SuggestionChips candidates={CONFLICT} conflict categories={CATEGORIES} onApply={vi.fn()} />,
    );

    const chips = screen.getAllByRole("button");
    expect(chips).toHaveLength(3);
    expect(chips[0]).toHaveTextContent("Restaurant › Dine in · your rule 'costco'");
    expect(chips[1]).toHaveTextContent("Grocery › Wholesale clubs · built-in");
    expect(chips[2]).toHaveTextContent("Fuel › Gasoline · LLM (saved)");
  });

  it("marks the current candidate with aria-pressed and a visible Current badge", () => {
    render(
      <SuggestionChips candidates={CONFLICT} conflict categories={CATEGORIES} onApply={vi.fn()} />,
    );

    const current = screen.getByRole("button", {
      name: "Restaurant › Dine in, your rule 'costco', current",
    });
    expect(current).toHaveAttribute("aria-pressed", "true");
    expect(current).toHaveTextContent("Current");
    const other = screen.getByRole("button", { name: "Grocery › Wholesale clubs, built-in" });
    expect(other).toHaveAttribute("aria-pressed", "false");
    expect(other).not.toHaveTextContent("Current");
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
        candidates={[
          candidate({
            source: "one_time_edit",
            category_key: "fuel",
            subcategory_key: "gasoline",
            label: "your one-time edit",
            rule_id: null,
            is_current: true,
          }),
          ...CONFLICT.slice(1, 2),
        ]}
        conflict={false}
        categories={CATEGORIES}
        onApply={onApply}
      />,
    );

    const edit = screen.getByRole("button", { name: "Fuel › Gasoline, your edit, current" });
    expect(edit).toHaveAttribute("aria-disabled", "true");
    await user.click(edit);
    expect(onApply).not.toHaveBeenCalled();
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
