import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import * as React from "react";
import { MemoryRouter, useSearchParams } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";

import {
  resolveSelectedUser,
  useCompareUser,
  SELECTED_USER_STORAGE_KEY,
  useSelectedUser,
} from "./use-selected-user";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, apiClient: { GET: vi.fn() } };
});

const USERS = [
  { id: 1, name: "Alex", is_default: true },
  { id: 2, name: "Sam", is_default: false },
];

describe("resolveSelectedUser", () => {
  it("URL wins over storage", () => {
    expect(resolveSelectedUser("2", "1", USERS)).toBe(2);
  });
  it("falls back to storage", () => {
    expect(resolveSelectedUser(null, "1", USERS)).toBe(1);
  });
  it("treats 'all' as an explicit null, even over storage", () => {
    expect(resolveSelectedUser("all", "1", USERS)).toBeNull();
    expect(resolveSelectedUser(null, "all", USERS)).toBeNull();
  });
  it("ignores stale or malformed ids", () => {
    expect(resolveSelectedUser("99", "1", USERS)).toBe(1);
    expect(resolveSelectedUser("abc", "99", USERS)).toBeNull();
  });
  it("with exactly one user, ignores 'all' from the URL or storage", () => {
    const one = USERS.slice(0, 1);
    expect(resolveSelectedUser("all", null, one)).toBe(1);
    expect(resolveSelectedUser(null, "all", one)).toBe(1);
    expect(resolveSelectedUser("all", "all", one)).toBe(1);
  });
  it("defaults to the only user, else All", () => {
    expect(resolveSelectedUser(null, null, USERS.slice(0, 1))).toBe(1);
    expect(resolveSelectedUser(null, null, USERS)).toBeNull();
    expect(resolveSelectedUser(null, null, [])).toBeNull();
  });
});

function makeWrapper(initialEntry: string) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter
          initialEntries={[initialEntry]}
          future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        >
          {children}
        </MemoryRouter>
      </QueryClientProvider>
    );
  };
}

function useBoth() {
  return { sel: useSelectedUser(), sp: useSearchParams() };
}

describe("useSelectedUser", () => {
  beforeEach(() => {
    window.localStorage.clear();
    (apiClient.GET as ReturnType<typeof vi.fn>).mockResolvedValue({ data: USERS, error: undefined });
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("reports loading, then resolves from the URL", async () => {
    const { result } = renderHook(useBoth, { wrapper: makeWrapper("/spending?user=2") });
    expect(result.current.sel.isLoading).toBe(true);
    expect(result.current.sel.selectedUserId).toBeNull();
    await waitFor(() => expect(result.current.sel.isLoading).toBe(false));
    expect(result.current.sel.selectedUserId).toBe(2);
    expect(result.current.sel.selectedUser?.name).toBe("Sam");
    expect(result.current.sel.userIds).toEqual([2]);
  });

  it("restores from localStorage when the URL has no param", async () => {
    window.localStorage.setItem(SELECTED_USER_STORAGE_KEY, "1");
    const { result } = renderHook(useBoth, { wrapper: makeWrapper("/spending") });
    await waitFor(() => expect(result.current.sel.selectedUserId).toBe(1));
  });

  it("restores a selection saved under the pre-rename key, moving it to the new key", async () => {
    window.localStorage.setItem("spend-analyzer:selected-user", "1");
    const { result } = renderHook(useBoth, { wrapper: makeWrapper("/spending") });
    await waitFor(() => expect(result.current.sel.selectedUserId).toBe(1));
    expect(window.localStorage.getItem(SELECTED_USER_STORAGE_KEY)).toBe("1");
    expect(window.localStorage.getItem("spend-analyzer:selected-user")).toBeNull();
  });

  it("prefers the new key over a leftover pre-rename one", async () => {
    window.localStorage.setItem(SELECTED_USER_STORAGE_KEY, "2");
    window.localStorage.setItem("spend-analyzer:selected-user", "1");
    const { result } = renderHook(useBoth, { wrapper: makeWrapper("/spending") });
    await waitFor(() => expect(result.current.sel.selectedUserId).toBe(2));
  });

  it("is All users (userIds undefined) by default with several users", async () => {
    const { result } = renderHook(useBoth, { wrapper: makeWrapper("/spending") });
    await waitFor(() => expect(result.current.sel.isLoading).toBe(false));
    expect(result.current.sel.selectedUserId).toBeNull();
    expect(result.current.sel.userIds).toBeUndefined();
  });

  it("does not crash when localStorage throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const { result } = renderHook(useBoth, { wrapper: makeWrapper("/spending") });
    await waitFor(() => expect(result.current.sel.isLoading).toBe(false));
    act(() => result.current.sel.setSelectedUserId(2));
    expect(result.current.sel.selectedUserId).toBe(2);
  });

  it("setSelectedUserId updates URL, keeps other params, and writes storage", async () => {
    const { result } = renderHook(useBoth, { wrapper: makeWrapper("/spending?date_from=2025-01-01") });
    await waitFor(() => expect(result.current.sel.isLoading).toBe(false));

    act(() => result.current.sel.setSelectedUserId(2));
    expect(result.current.sp[0].get("user")).toBe("2");
    expect(result.current.sp[0].get("date_from")).toBe("2025-01-01");
    expect(window.localStorage.getItem(SELECTED_USER_STORAGE_KEY)).toBe("2");
    expect(result.current.sel.selectedUserId).toBe(2);

    act(() => result.current.sel.setSelectedUserId(null));
    expect(result.current.sp[0].get("user")).toBe("all");
    expect(window.localStorage.getItem(SELECTED_USER_STORAGE_KEY)).toBe("all");
    expect(result.current.sel.selectedUserId).toBeNull();
  });
});

describe("useCompareUser", () => {
  function useCompare(primary: number | null) {
    return { compare: useCompareUser(primary, USERS, false), sp: useSearchParams() };
  }

  it("drops ?compare from the URL when it equals the primary user", async () => {
    const { result } = renderHook(() => useCompare(1), { wrapper: makeWrapper("/spending?user=1&compare=1") });
    await waitFor(() => expect(result.current.sp[0].get("compare")).toBeNull());
    expect(result.current.compare.active).toBe(false);
    expect(result.current.sp[0].get("user")).toBe("1");
  });

  it("keeps a valid ?compare", () => {
    const { result } = renderHook(() => useCompare(1), { wrapper: makeWrapper("/spending?user=1&compare=2") });
    expect(result.current.compare.active).toBe(true);
    expect(result.current.sp[0].get("compare")).toBe("2");
  });
});
