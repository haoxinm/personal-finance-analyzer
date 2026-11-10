import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createApiFetchMock, jsonResponse, makeTransaction, type AcceptBody } from "./test-support";
import type { Transaction } from "./types";

/**
 * `apiClient` (`api/client.ts`) is a module-level `openapi-fetch` client built once, at import
 * time, from whatever `fetch` is global then (see `api/client.test.ts`, which does the same for
 * the same reason). So every test here stubs `fetch` and resets the module registry *before*
 * importing `useReviewQueue` — and, for the same reason, imports `ToastContextProvider`
 * fresh too: a stale copy would hold a different `ToastContext` object than the one the reloaded
 * `useReviewQueue` (via `useToast`) reads, and `useContext` would come back empty.
 */
async function setup(options: {
  transactions: Transaction[];
  acceptBehavior?: (body: AcceptBody) => Response;
  undoBehavior?: Parameters<typeof createApiFetchMock>[0]["undoBehavior"];
}) {
  vi.resetModules();
  const fetchMock = vi.fn(
    createApiFetchMock({
      transactions: options.transactions,
      acceptBehavior: options.acceptBehavior,
      undoBehavior: options.undoBehavior,
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  const { useReviewQueue } = await import("./use-review-queue");
  const { ToastContextProvider } = await import("@/components/ui/toast-provider");
  const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
  const React = await import("react");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(
      QueryClientProvider,
      { client: queryClient },
      React.createElement(ToastContextProvider, null, children),
    );
  return { fetchMock, useReviewQueue, wrapper, queryClient };
}

type FetchMock = Awaited<ReturnType<typeof setup>>["fetchMock"];

function callsTo(fetchMock: FetchMock, method: string, pathname: string): Request[] {
  return fetchMock.mock.calls
    .map((call) => call[0] as Request)
    .filter((request) => request.method === method && new URL(request.url).pathname === pathname);
}

async function bodyOf(request: Request | undefined): Promise<Record<string, unknown>> {
  if (!request) throw new Error("expected a request");
  return JSON.parse(await request.clone().text()) as Record<string, unknown>;
}

describe("useReviewQueue", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("loads only the needs_review transactions, across pages", async () => {
    const { useReviewQueue, wrapper } = await setup({
      transactions: [
        makeTransaction({ id: 1, needs_review: true }),
        makeTransaction({ id: 2, needs_review: false }),
        makeTransaction({ id: 3, needs_review: true }),
      ],
    });

    const { result } = renderHook(() => useReviewQueue(), { wrapper });

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.queue.map((t) => t.id)).toEqual([1, 3]);
    expect(result.current.current?.id).toBe(1);
  });

  it("j/k move the cursor and clamp at the ends", async () => {
    const { useReviewQueue, wrapper } = await setup({
      transactions: [makeTransaction({ id: 1 }), makeTransaction({ id: 2 })],
    });

    const { result } = renderHook(() => useReviewQueue(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    act(() => result.current.previous());
    expect(result.current.current?.id).toBe(1); // clamped, no earlier row

    act(() => result.current.next());
    expect(result.current.current?.id).toBe(2);

    act(() => result.current.next());
    expect(result.current.current?.id).toBe(2); // clamped, no later row
  });

  it("accept sends the staged pick and optimistically removes the row", async () => {
    const { fetchMock, useReviewQueue, wrapper } = await setup({
      transactions: [makeTransaction({ id: 1, category_key: "others", subcategory_key: "uncategorized" })],
    });

    const { result } = renderHook(() => useReviewQueue(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    act(() => result.current.stage("dining", "restaurants"));
    expect(result.current.stagedForCurrent).toEqual({ categoryKey: "dining", subcategoryKey: "restaurants" });

    await act(async () => {
      await result.current.accept();
    });

    expect(result.current.queue).toHaveLength(0);
    expect(result.current.canUndo).toBe(true);

    const acceptCalls = callsTo(fetchMock, "POST", "/api/review/accept");
    expect(acceptCalls).toHaveLength(1);
    expect(await bodyOf(acceptCalls[0])).toMatchObject({
      transaction_id: 1,
      category_key: "dining",
      subcategory_key: "restaurants",
      create_rule: false,
      similar_ids: [],
    });
  });

  it("accept falls back to the cascade's own proposal when nothing is staged", async () => {
    const { useReviewQueue, wrapper } = await setup({
      transactions: [makeTransaction({ id: 1, category_key: "groceries", subcategory_key: "supermarket" })],
    });

    const { result } = renderHook(() => useReviewQueue(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.accept();
    });

    expect(result.current.queue).toHaveLength(0);
  });

  it("accept with createRule sends create_rule: true", async () => {
    const { fetchMock, useReviewQueue, wrapper } = await setup({
      transactions: [makeTransaction({ id: 1, category_key: "groceries", subcategory_key: "supermarket" })],
    });

    const { result } = renderHook(() => useReviewQueue(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.accept({ createRule: true });
    });

    const body = await bodyOf(callsTo(fetchMock, "POST", "/api/review/accept")[0]);
    expect(body.create_rule).toBe(true);
  });

  it("refuses to accept when there is no category to accept, without calling the server", async () => {
    const { fetchMock, useReviewQueue, wrapper } = await setup({
      transactions: [makeTransaction({ id: 1, category_key: null, subcategory_key: null })],
    });

    const { result } = renderHook(() => useReviewQueue(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.accept();
    });

    expect(result.current.queue).toHaveLength(1); // still there
    expect(callsTo(fetchMock, "POST", "/api/review/accept")).toHaveLength(0);
  });

  it("rolls back an accept that fails: the row reappears at its original position", async () => {
    const { fetchMock, useReviewQueue, wrapper } = await setup({
      transactions: [
        makeTransaction({ id: 1 }),
        makeTransaction({ id: 2, category_key: "groceries", subcategory_key: "supermarket" }),
        makeTransaction({ id: 3 }),
      ],
      acceptBehavior: () => jsonResponse({ detail: "server exploded" }, 500),
    });

    const { result } = renderHook(() => useReviewQueue(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    act(() => result.current.next()); // move to transaction 2
    expect(result.current.current?.id).toBe(2);

    await act(async () => {
      await result.current.accept();
    });

    // Rolled back: still 3 rows, transaction 2 back in the middle, cursor back on it.
    expect(result.current.queue.map((t) => t.id)).toEqual([1, 2, 3]);
    expect(result.current.current?.id).toBe(2);
    expect(result.current.canUndo).toBe(false);
    expect(callsTo(fetchMock, "POST", "/api/review/accept")).toHaveLength(1);
  });

  it("undo asks the server to revert the accept with its token and reinserts the row", async () => {
    const { fetchMock, useReviewQueue, wrapper } = await setup({
      transactions: [
        makeTransaction({ id: 1, category_key: null, subcategory_key: null, kind: "purchase" }),
        makeTransaction({ id: 2 }),
      ],
    });

    const { result } = renderHook(() => useReviewQueue(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    act(() => result.current.stage("dining", "restaurants"));
    await act(async () => {
      await result.current.accept();
    });
    expect(result.current.queue.map((t) => t.id)).toEqual([2]);

    await act(async () => {
      await result.current.undo();
    });

    // Back in the queue, still flagged for review — including a row that had no category at all.
    expect(result.current.queue.map((t) => t.id)).toEqual([1, 2]);
    expect(result.current.queue[0]).toMatchObject({ category_key: null, needs_review: true });
    expect(result.current.canUndo).toBe(false);

    // The server's own undo token went back verbatim, not a client-side PATCH approximation.
    const accepted = callsTo(fetchMock, "POST", "/api/review/accept");
    const undone = callsTo(fetchMock, "POST", "/api/review/undo");
    expect(undone).toHaveLength(1);
    const undoBody = await bodyOf(undone[0]);
    expect(undoBody.transactions).toHaveLength(1);
    expect((undoBody.transactions as unknown[])[0]).toMatchObject({
      transaction_id: 1,
      before: { needs_review: true },
      after: { needs_review: false },
    });
    expect(accepted).toHaveLength(1);
    expect(fetchMock.mock.calls.some((call) => (call[0] as Request).method === "PATCH")).toBe(false);
  });

  it("offers to apply an accept to the merchant's other queue rows, and undo restores them all", async () => {
    const { fetchMock, useReviewQueue, wrapper } = await setup({
      transactions: [
        makeTransaction({ id: 1, merchant_key: "trader joes", category_key: "groceries", subcategory_key: "supermarket" }),
        makeTransaction({ id: 2, merchant_key: "shell" }),
        makeTransaction({ id: 3, merchant_key: "trader joes" }),
        makeTransaction({ id: 4, merchant_key: "trader joes" }),
        makeTransaction({ id: 5, merchant_key: "" }),
        makeTransaction({ id: 6, merchant_key: "" }),
      ],
    });

    const { result } = renderHook(() => useReviewQueue(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    // Same non-empty merchant only: rows with no merchant key never group together.
    expect(result.current.similarForCurrent.map((t) => t.id)).toEqual([3, 4]);
    expect(result.current.applyToSimilar).toBe(true);

    await act(async () => {
      await result.current.accept();
    });

    expect((await bodyOf(callsTo(fetchMock, "POST", "/api/review/accept")[0])).similar_ids).toEqual([3, 4]);
    expect(result.current.queue.map((t) => t.id)).toEqual([2, 5, 6]);

    await act(async () => {
      await result.current.undo();
    });
    expect(result.current.queue.map((t) => t.id)).toEqual([1, 2, 3, 4, 5, 6]);
  });

  it("does not re-queue a row the server skipped because it was edited after the accept", async () => {
    const { useReviewQueue, wrapper } = await setup({
      transactions: [
        makeTransaction({ id: 1, merchant_key: "trader joes", category_key: "groceries", subcategory_key: "supermarket" }),
        makeTransaction({ id: 2, merchant_key: "trader joes" }),
        makeTransaction({ id: 3, merchant_key: "trader joes" }),
      ],
      // Row 3 was re-categorized elsewhere after the accept: the server leaves it as it is.
      undoBehavior: () => ({ restored: 2, skipped: [3], rule_skipped: false }),
    });
    const { ToastContext } = await import("@/components/ui/toast-context");
    const React = await import("react");

    const { result } = renderHook(
      () => ({ queue: useReviewQueue(), toasts: React.useContext(ToastContext)?.toasts ?? [] }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.queue.status).toBe("ready"));
    await act(async () => {
      await result.current.queue.accept();
    });
    expect(result.current.queue.queue).toEqual([]);

    await act(async () => {
      await result.current.queue.undo();
    });

    expect(result.current.queue.queue.map((t) => t.id)).toEqual([1, 2]);
    const last = result.current.toasts.at(-1);
    expect(last?.title).toBe("Undone — 2 transactions restored");
    const description = typeof last?.description === "string" ? last.description : "";
    expect(description).toContain("1 transaction was edited since");
  });

  it("refreshes the Transactions and analytics caches after an accept and after an undo", async () => {
    const { useReviewQueue, wrapper, queryClient } = await setup({
      transactions: [makeTransaction({ id: 1 }), makeTransaction({ id: 2 })],
    });
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    const { result } = renderHook(() => useReviewQueue(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    act(() => result.current.stage("dining", "restaurants"));
    await act(async () => {
      await result.current.accept();
    });
    const keys = () => invalidate.mock.calls.map((call) => JSON.stringify(call[0]?.queryKey));
    expect(keys()).toEqual(expect.arrayContaining(['["transactions"]', '["analytics"]']));

    invalidate.mockClear();
    await act(async () => {
      await result.current.undo();
    });
    expect(keys()).toEqual(expect.arrayContaining(['["transactions"]', '["analytics"]']));
  });

  it("leaves the other rows alone when apply-to-similar is switched off", async () => {
    const { fetchMock, useReviewQueue, wrapper } = await setup({
      transactions: [
        makeTransaction({ id: 1, merchant_key: "trader joes", category_key: "groceries", subcategory_key: "supermarket" }),
        makeTransaction({ id: 2, merchant_key: "trader joes" }),
      ],
    });

    const { result } = renderHook(() => useReviewQueue(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    act(() => result.current.setApplyToSimilar(false));

    await act(async () => {
      await result.current.accept();
    });

    expect((await bodyOf(callsTo(fetchMock, "POST", "/api/review/accept")[0])).similar_ids).toEqual([]);
    expect(result.current.queue.map((t) => t.id)).toEqual([2]);
  });

  it("keeps rows the server declined to apply in the queue", async () => {
    const { useReviewQueue, wrapper } = await setup({
      transactions: [
        makeTransaction({ id: 1, merchant_key: "trader joes", category_key: "groceries", subcategory_key: "supermarket" }),
        makeTransaction({ id: 2, merchant_key: "trader joes" }),
        makeTransaction({ id: 3, merchant_key: "trader joes" }),
      ],
      // Row 3 was corrected elsewhere in the meantime: the server only applies to row 2.
      acceptBehavior: () =>
        jsonResponse({
          transaction: makeTransaction({ id: 1, needs_review: false }),
          similar_ids: [2],
          undo: { transactions: [], rule: null },
        }),
    });

    const { result } = renderHook(() => useReviewQueue(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    await act(async () => {
      await result.current.accept();
    });

    expect(result.current.queue.map((t) => t.id)).toEqual([3]);
  });

  it("passes user_ids to the query and refetches when they change", async () => {
    const { fetchMock, useReviewQueue, wrapper } = await setup({
      transactions: [makeTransaction({ id: 1 })],
    });

    const initialIds: { ids: number[] | undefined } = { ids: [2] };
    const { result, rerender } = renderHook(({ ids }: { ids: number[] | undefined }) => useReviewQueue(ids), {
      wrapper,
      initialProps: initialIds,
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    const idsOf = () =>
      fetchMock.mock.calls
        .map((c) => new URL((c[0] as Request).url))
        .filter((u) => u.pathname === "/api/transactions")
        .map((u) => u.searchParams.getAll("user_ids").join());
    expect(idsOf()).toEqual(["2"]);

    rerender({ ids: [2] }); // new array, same content: no refetch
    rerender({ ids: [3] });
    await waitFor(() => expect(idsOf()).toEqual(["2", "3"]));

    rerender({ ids: undefined });
    await waitFor(() => expect(idsOf()).toEqual(["2", "3", ""]));
  });

  it("does not load until enabled", async () => {
    const { fetchMock, useReviewQueue, wrapper } = await setup({ transactions: [makeTransaction({ id: 1 })] });
    const { result, rerender } = renderHook(({ on }: { on: boolean }) => useReviewQueue(undefined, on), {
      wrapper,
      initialProps: { on: false },
    });
    expect(result.current.status).toBe("loading");
    expect(fetchMock).not.toHaveBeenCalled();
    rerender({ on: true });
    await waitFor(() => expect(result.current.status).toBe("ready"));
  });
});
