import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { toTransactionsQuery } from "./api";

describe("toTransactionsQuery", () => {
  it("maps the SpendQueryParams subset onto GET /transactions's own query names", () => {
    expect(
      toTransactionsQuery({
        userIds: [1, 2],
        dateFrom: "2026-01-01",
        dateTo: "2026-01-31",
        categoryKeys: ["groceries"],
        amountMinMinor: 100,
        kinds: ["purchase"],
        search: "coffee",
      }),
    ).toEqual({
      user_ids: [1, 2],
      account_ids: undefined,
      date_from: "2026-01-01",
      date_to: "2026-01-31",
      category_keys: ["groceries"],
      subcategory_keys: undefined,
      amount_min_minor: 100,
      amount_max_minor: undefined,
      kinds: ["purchase"],
      include_non_spend: undefined,
      search: "coffee",
      currency: undefined,
    });
  });

  it("drops analytics-only fields (granularity, groupBy, netRefunds) that GET /transactions doesn't accept", () => {
    const query = toTransactionsQuery({
      granularity: "month",
      groupBy: ["category"],
      netRefunds: true,
    } as never);
    expect(query).not.toHaveProperty("granularity");
    expect(query).not.toHaveProperty("group_by");
    expect(query).not.toHaveProperty("net_refunds");
  });
});

describe("toTransactionsQuery grid filters", () => {
  it("adds the grid-only filters when given, and nothing when not", () => {
    const query = toTransactionsQuery(
      {},
      { needsReview: true, uncategorized: true, statementId: 9, sort: "-posted_date" },
    );
    expect(query).toMatchObject({ needs_review: true, uncategorized: true, statement_id: 9 });
    expect(query).not.toHaveProperty("sort");
    expect(toTransactionsQuery({})).not.toHaveProperty("needs_review");
  });

  it("carries the conflicting-suggestions filter as ?conflict=", () => {
    expect(toTransactionsQuery({}, { conflict: true, sort: "-posted_date" })).toMatchObject({
      conflict: true,
    });
    expect(toTransactionsQuery({}, { sort: "-posted_date" }).conflict).toBeUndefined();
  });
});

describe("downloadTransactionsExport", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("fetches the export and triggers a same-shaped file download", async () => {
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(
        new Response("id,amount_minor\n1,1234\n", {
          status: 200,
          headers: { "Content-Type": "text/csv" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    // jsdom doesn't implement these; define them before spying so `vi.spyOn` has something
    // to wrap.
    if (!URL.createObjectURL) URL.createObjectURL = () => "";
    if (!URL.revokeObjectURL) URL.revokeObjectURL = () => {};
    const createObjectURL = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:mock");
    const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});

    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    const { downloadTransactionsExport } = await import("./api");
    await downloadTransactionsExport("csv");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const request = fetchMock.mock.calls.at(0)?.[0] as Request | undefined;
    expect(request?.url).toContain("/api/export");
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock");
  });

  it("sends the grid's filters and selected user, not an unfiltered export", async () => {
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response("id\n", { status: 200, headers: { "Content-Type": "text/csv" } })),
    );
    vi.stubGlobal("fetch", fetchMock);
    if (!URL.createObjectURL) URL.createObjectURL = () => "";
    if (!URL.revokeObjectURL) URL.revokeObjectURL = () => {};
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:mock");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    const { downloadTransactionsExport } = await import("./api");
    await downloadTransactionsExport(
      "csv",
      { userIds: [2], dateFrom: "2026-01-01", search: "coffee", accountIds: [4] },
      { needsReview: true, sort: "-posted_date" },
    );

    const request = fetchMock.mock.calls.at(0)?.[0] as Request;
    const params = new URL(request.url).searchParams;
    expect(params.getAll("user_ids")).toEqual(["2"]);
    expect(params.getAll("account_ids")).toEqual(["4"]);
    expect(params.get("date_from")).toBe("2026-01-01");
    expect(params.get("search")).toBe("coffee");
    expect(params.get("needs_review")).toBe("true");
    // Mirrors what the grid shows (spend kinds, USD) rather than the server's "everything".
    expect(params.get("include_non_spend")).toBe("false");
    expect(params.get("currency")).toBe("USD");
    expect(params.get("format")).toBe("csv");
  });
});
