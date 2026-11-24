import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

describe("usePatchTransaction", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("refreshes everything a corrected date or amount feeds, including tax and net worth", async () => {
    vi.resetModules();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify({ id: 1 }), { status: 200, headers: { "Content-Type": "application/json" } }))),
    );
    const { usePatchTransaction } = await import("./hooks");
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => usePatchTransaction(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ id: 1, body: { notes: "x", create_rule: false } });
    });
    await waitFor(() => expect(invalidate).toHaveBeenCalled());
    const keys = invalidate.mock.calls.map((c) => (c[0]?.queryKey as string[])[0]);
    expect(keys).toEqual(
      expect.arrayContaining(["transactions", "analytics", "dashboard", "review", "statements", "net-worth", "tax"]),
    );
  });
});
