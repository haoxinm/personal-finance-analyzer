import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { components } from "@/api/client";
import { Toaster } from "@/components/ui/toaster";

import { RulesTab } from "./rules-tab";
import { type ApiHandler, setMockHandlers, TestProviders } from "./test-utils";

vi.mock("@/api/client", async () => {
  const { dynamicMockFetch } = await import("./test-utils");
  vi.stubGlobal("fetch", dynamicMockFetch);
  return vi.importActual("@/api/client");
});

type Rule = components["schemas"]["Rule"];
type Category = components["schemas"]["Category"];

const TAXONOMY: Category[] = [
  {
    key: "food",
    name: "Food",
    subcategories: [
      { id: 1, key: "coffee_shops", name: "Coffee shops", pending: false },
      { id: 2, key: "groceries", name: "Groceries", pending: false },
    ],
  },
];

const RULES: Rule[] = [
  {
    id: 1,
    pattern: "starbucks",
    category_key: "food",
    subcategory_key: "coffee_shops",
    kind: null,
    source: "user",
    match_type: "contains",
    priority: 100,
  },
  {
    id: 2,
    pattern: "trader joe",
    category_key: "food",
    subcategory_key: "groceries",
    kind: null,
    source: "builtin",
    match_type: "contains",
    priority: 100,
  },
];

function setUpHandlers(overrides: Partial<Record<string, ApiHandler>> = {}) {
  const defaults: Record<string, ApiHandler> = {
    "GET /rules": () => RULES,
    "GET /taxonomy": () => TAXONOMY,
    "GET /transactions": () => ({ items: [], total: 0 }),
  };
  // The mock fetch takes the first key whose path is a substring of the request's, so the more
  // specific overrides (e.g. `GET /apply-preview`) must come before `GET /rules`.
  const handlers: Record<string, ApiHandler> = {};
  for (const [key, handler] of Object.entries(overrides)) {
    if (handler) handlers[key] = handler;
  }
  for (const [key, handler] of Object.entries(defaults)) {
    handlers[key] ??= handler;
  }
  setMockHandlers(handlers);
}

describe("RulesTab", () => {
  beforeEach(() => {
    setUpHandlers();
  });

  it("lists existing rules with their source badge", async () => {
    render(<RulesTab />, { wrapper: TestProviders });

    expect(await screen.findByText("starbucks")).toBeInTheDocument();
    expect(screen.getByText("trader joe")).toBeInTheDocument();
    expect(screen.getByText("Rules (2)")).toBeInTheDocument();
  });

  it("does not offer edit/delete for a builtin rule", async () => {
    render(<RulesTab />, { wrapper: TestProviders });
    await screen.findByText("trader joe");

    expect(
      screen.getByText("Built-in rules are managed in the corpus, not here."),
    ).toBeInTheDocument();
  });

  it("shows a live match count for a valid contains pattern", async () => {
    setUpHandlers({
      "GET /transactions": () => ({
        items: [
          {
            id: 1,
            statement_id: 1,
            account_id: 1,
            posted_date: "2026-01-01",
            transaction_date: null,
            description_clean: "x",
            amount_minor: 100,
            currency: "USD",
            kind: "purchase",
            category_key: null,
            subcategory_key: null,
            merchant_key: "starbucks 4102",
            notes: null,
            needs_review: false,
          },
          {
            id: 2,
            statement_id: 1,
            account_id: 1,
            posted_date: "2026-01-02",
            transaction_date: null,
            description_clean: "y",
            amount_minor: 200,
            currency: "USD",
            kind: "purchase",
            category_key: null,
            subcategory_key: null,
            merchant_key: "trader joes",
            notes: null,
            needs_review: false,
          },
        ],
        total: 2,
      }),
    });
    const user = userEvent.setup();
    render(<RulesTab />, { wrapper: TestProviders });
    await screen.findByText("starbucks");

    await user.type(screen.getByLabelText("Pattern"), "starbucks");

    await waitFor(
      () => {
        expect(screen.getByTestId("match-preview").textContent).toContain("Matches 1 of 2");
      },
      { timeout: 2000 },
    );
  });

  it("shows a validation error for a broken regex instead of a match count", async () => {
    const user = userEvent.setup();
    render(<RulesTab />, { wrapper: TestProviders });
    await screen.findByText("starbucks");

    await user.selectOptions(screen.getByLabelText("Match type"), "regex");
    await user.type(screen.getByLabelText("Pattern"), "(unterminated");

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/./);
    });
  });

  it("disables submit until a category/subcategory is chosen", async () => {
    const user = userEvent.setup();
    render(<RulesTab />, { wrapper: TestProviders });
    await screen.findByText("starbucks");

    await user.type(screen.getByLabelText("Pattern"), "costco");
    expect(screen.getByRole("button", { name: "Add rule" })).toBeDisabled();

    await user.selectOptions(screen.getByLabelText("Category / subcategory"), "food::groceries");
    expect(screen.getByRole("button", { name: "Add rule" })).toBeEnabled();
  });

  it("filters the rule list by a search term matching the pattern or the category", async () => {
    const user = userEvent.setup();
    render(<RulesTab />, { wrapper: TestProviders });
    await screen.findByText("starbucks");
    expect(screen.getByText("trader joe")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Search rules"), "starbucks");

    expect(screen.getByText("starbucks")).toBeInTheDocument();
    expect(screen.queryByText("trader joe")).not.toBeInTheDocument();
    expect(screen.getByText("Rules (1 of 2)")).toBeInTheDocument();
  });

  it("shows a no-match message and no rows for a search with no hits", async () => {
    const user = userEvent.setup();
    render(<RulesTab />, { wrapper: TestProviders });
    await screen.findByText("starbucks");

    await user.type(screen.getByLabelText("Search rules"), "zzz-nomatch");

    expect(screen.getByText(/No rules match/)).toBeInTheDocument();
    expect(screen.queryByText("starbucks")).not.toBeInTheDocument();
  });
});

const NO_UNDO = { transactions: [], rule: null };

describe("RulesTab precedence and retroactive apply", () => {
  const CREATED: Rule = {
    id: 9,
    pattern: "costco",
    category_key: "food",
    subcategory_key: "groceries",
    kind: null,
    source: "user",
    match_type: "contains",
    priority: 100,
  };

  async function createCostcoRule(
    user: ReturnType<typeof userEvent.setup>,
    existingMatches: number | null,
  ) {
    const applied: number[] = [];
    setUpHandlers({
      "GET /apply-preview": () => ({
        matches: existingMatches ?? 0,
        user_count: 1,
        kind_changes: 0,
        all_users: true,
      }),
      "POST /apply": (req) => {
        applied.push(Number(new URL(req.url).pathname.split("/").at(-2)));
        return { updated: 4, undo: NO_UNDO };
      },
      "POST /rules": () => ({ ...CREATED, existing_matches: existingMatches }),
    });
    render(<RulesTab />, { wrapper: TestProviders });
    await screen.findByText("starbucks");
    await user.type(screen.getByLabelText("Pattern"), "costco");
    await user.selectOptions(screen.getByLabelText("Category / subcategory"), "food::groceries");
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    return applied;
  }

  it("shows each rule's match type and priority, and the order rules are consulted in", async () => {
    render(<RulesTab />, { wrapper: TestProviders });
    await screen.findByText("starbucks");

    expect(screen.getAllByTestId("rule-precedence")[0]).toHaveTextContent("contains match · priority 100");
    expect(screen.getByTestId("precedence-note")).toHaveTextContent(
      "one-time edits to a transaction > your own rules (lower priority number wins; ties go to the older rule) > built-in rules > issuer category and merchant cache > LLM",
    );
    expect(screen.getByTestId("precedence-note")).toHaveTextContent(
      "A transaction you edited by hand is never changed by a rule until you choose",
    );
    expect(screen.getByTestId("precedence-note")).toHaveTextContent(
      "one click uses a lower one for that transaction only",
    );
    expect(screen.getByTestId("precedence-note")).toHaveTextContent(
      "until you choose “Revert to automatic”",
    );
  });

  it("offers to apply a new rule to the transactions it matches, and applies it on request", async () => {
    const user = userEvent.setup();
    const applied = await createCostcoRule(user, 4);

    const offer = await screen.findByTestId("apply-offer");
    expect(offer).toHaveTextContent("also matches 4 transactions you already have");
    await user.click(screen.getByRole("button", { name: "Apply to 4 existing" }));

    // One click only asks; nothing is applied until the confirmation.
    const confirm = await screen.findByRole("alertdialog", { name: "Confirm apply rule" });
    expect(confirm).toHaveTextContent("4 existing transactions");
    expect(applied).toEqual([]);
    await user.click(within(confirm).getByRole("button", { name: "Yes, apply to 4" }));

    await waitFor(() => expect(applied).toEqual([9]));
    await waitFor(() => expect(screen.queryByTestId("apply-offer")).not.toBeInTheDocument());
  });

  it("lets the offer be dismissed without applying anything", async () => {
    const user = userEvent.setup();
    const applied = await createCostcoRule(user, 4);

    await screen.findByTestId("apply-offer");
    await user.click(screen.getByRole("button", { name: "Not now" }));

    expect(screen.queryByTestId("apply-offer")).not.toBeInTheDocument();
    expect(applied).toEqual([]);
  });

  it("makes no offer when the new rule matches nothing already imported", async () => {
    const user = userEvent.setup();
    await createCostcoRule(user, 0);
    await waitFor(() => expect(screen.getByLabelText("Pattern")).toHaveValue(""));
    expect(screen.queryByTestId("apply-offer")).not.toBeInTheDocument();
  });

  it("applies an existing user rule from its row, but offers no such action on a built-in one", async () => {
    const user = userEvent.setup();
    const applied: number[] = [];
    setUpHandlers({
      "GET /apply-preview": () => ({ matches: 2, user_count: 1, kind_changes: 0, all_users: false }),
      "POST /apply": (req) => {
        applied.push(Number(new URL(req.url).pathname.split("/").at(-2)));
        return { updated: 2, undo: NO_UNDO };
      },
    });
    render(<RulesTab />, { wrapper: TestProviders });
    await screen.findByText("starbucks");

    expect(screen.getAllByRole("button", { name: "Apply to existing" })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Apply to existing" }));
    await user.click(await screen.findByRole("button", { name: "Yes, apply to 2" }));
    await waitFor(() => expect(applied).toEqual([1]));
  });

  it("shows the match count, and warns about cross-user and kind effects, before applying", async () => {
    const user = userEvent.setup();
    const applied: number[] = [];
    setUpHandlers({
      "GET /apply-preview": () => ({ matches: 7, user_count: 2, kind_changes: 3, all_users: true }),
      "POST /apply": () => {
        applied.push(1);
        return { updated: 7, undo: NO_UNDO };
      },
      "GET /rules": () => [{ ...RULES[0]!, kind: "transfer" }, RULES[1]!],
    });
    render(<RulesTab />, { wrapper: TestProviders });
    await screen.findByText("starbucks");

    await user.click(screen.getByRole("button", { name: "Apply to existing" }));
    const confirm = await screen.findByRole("alertdialog", { name: "Confirm apply rule" });
    expect(confirm).toHaveTextContent("7 existing transactions");
    expect(screen.getByTestId("apply-scope")).toHaveTextContent(
      "applies to every user\u2019s transactions (these belong to 2 users)",
    );
    expect(screen.getByTestId("apply-kind")).toHaveTextContent("3 transactions will also change kind to transfer");

    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(applied).toEqual([]);
  });

  it("says so and asks nothing when the rule would change nothing", async () => {
    const user = userEvent.setup();
    setUpHandlers({
      "GET /apply-preview": () => ({ matches: 0, user_count: 0, kind_changes: 0, all_users: false }),
    });
    render(
      <>
        <RulesTab />
        <Toaster />
      </>,
      { wrapper: TestProviders },
    );
    await screen.findByText("starbucks");

    await user.click(screen.getByRole("button", { name: "Apply to existing" }));
    expect(await screen.findByText("Nothing to update")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("offers Undo after applying, and sends the server's undo token back", async () => {
    const user = userEvent.setup();
    const token = {
      transactions: [
        {
          transaction_id: 5,
          before: {
            category_id: null,
            subcategory_id: null,
            confidence: null,
            classified_by: "llm",
            needs_review: true,
            kind: "purchase",
            is_spend: true,
            merchant_canonical: null,
          },
          after: {
            category_id: 2,
            subcategory_id: 2,
            confidence: 1,
            classified_by: "user_rule",
            needs_review: false,
            kind: "purchase",
            is_spend: true,
            merchant_canonical: null,
          },
        },
      ],
      rule: null,
    };
    const undone: unknown[] = [];
    setUpHandlers({
      "GET /apply-preview": () => ({ matches: 1, user_count: 1, kind_changes: 0, all_users: false }),
      "POST /apply": () => ({ updated: 1, undo: token }),
      "POST /review/undo": async (req) => {
        undone.push(await req.clone().json());
        return { restored: 1, skipped: [], rule_skipped: false };
      },
    });
    render(
      <>
        <RulesTab />
        <Toaster />
      </>,
      { wrapper: TestProviders },
    );
    await screen.findByText("starbucks");

    await user.click(screen.getByRole("button", { name: "Apply to existing" }));
    await user.click(await screen.findByRole("button", { name: "Yes, apply to 1" }));
    expect(await screen.findByText("Updated 1 existing transaction")).toBeInTheDocument();

    // `fireEvent`: Radix's toast action tracks pointer capture, which jsdom does not implement.
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(undone).toEqual([token]));
    expect(await screen.findByText("Undone \u2014 1 transaction restored")).toBeInTheDocument();
  });
});

/** With ~120 built-in rules (§ the corpus) plus any the user adds, this list needs its own
 * paging so it never dumps every row onto one enormous page (a real usability bug — the whole
 * screen used to render as one ~8600px-tall page). */
describe("RulesTab pagination", () => {
  function manyRules(count: number): Rule[] {
    return Array.from({ length: count }, (_, i) => ({
      id: i + 1,
      pattern: `merchant-${String(i + 1).padStart(3, "0")}`,
      category_key: "food",
      subcategory_key: "groceries",
      kind: null,
      source: "builtin" as const,
      match_type: "contains",
      priority: 100,
    }));
  }

  beforeEach(() => {
    setUpHandlers({ "GET /rules": () => manyRules(60) });
  });

  it("shows only the first page's worth of rules, with page controls", async () => {
    render(<RulesTab />, { wrapper: TestProviders });

    await screen.findByText("merchant-001");
    expect(screen.getByText("merchant-025")).toBeInTheDocument();
    expect(screen.queryByText("merchant-026")).not.toBeInTheDocument();
    expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
  });

  it("advances to the next page", async () => {
    const user = userEvent.setup();
    render(<RulesTab />, { wrapper: TestProviders });

    await screen.findByText("merchant-001");
    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(screen.getByText("merchant-026")).toBeInTheDocument();
    expect(screen.queryByText("merchant-001")).not.toBeInTheDocument();
    expect(screen.getByText("Page 2 of 3")).toBeInTheDocument();
  });

  it("resets to page 1 when the search term changes", async () => {
    const user = userEvent.setup();
    render(<RulesTab />, { wrapper: TestProviders });

    await screen.findByText("merchant-001");
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("Page 2 of 3")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Search rules"), "merchant-05");

    expect(screen.getByText("merchant-050")).toBeInTheDocument();
    expect(screen.queryByText("Page 2")).not.toBeInTheDocument();
  });
});
