import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePersistentChoice } from "./use-persistent-choice";

const ALLOWED = ["a", "b", "c"] as const;

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("usePersistentChoice", () => {
  it("returns the fallback by default and persists a choice", () => {
    const { result } = renderHook(() => usePersistentChoice("pfa:test:one", ALLOWED, "a"));
    expect(result.current[0]).toBe("a");
    act(() => result.current[1]("b"));
    expect(result.current[0]).toBe("b");
    expect(window.localStorage.getItem("pfa:test:one")).toBe("b");
  });

  it("reads a stored value on mount", () => {
    window.localStorage.setItem("pfa:test:two", "c");
    const { result } = renderHook(() => usePersistentChoice("pfa:test:two", ALLOWED, "a"));
    expect(result.current[0]).toBe("c");
  });

  it("falls back when the stored value is not allowed", () => {
    window.localStorage.setItem("pfa:test:three", "zzz");
    const { result } = renderHook(() => usePersistentChoice("pfa:test:three", ALLOWED, "a"));
    expect(result.current[0]).toBe("a");
  });

  it("keeps two hook instances with the same key in sync", () => {
    const first = renderHook(() => usePersistentChoice("pfa:test:four", ALLOWED, "a"));
    const second = renderHook(() => usePersistentChoice("pfa:test:four", ALLOWED, "a"));
    act(() => first.result.current[1]("c"));
    expect(second.result.current[0]).toBe("c");
  });

  it("follows a storage event from another tab", () => {
    const { result } = renderHook(() => usePersistentChoice("pfa:test:five", ALLOWED, "a"));
    act(() => {
      window.localStorage.setItem("pfa:test:five", "b");
      window.dispatchEvent(new StorageEvent("storage", { key: "pfa:test:five", newValue: "b" }));
    });
    expect(result.current[0]).toBe("b");
  });

  it("still works in memory when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const { result } = renderHook(() => usePersistentChoice("pfa:test:six", ALLOWED, "a"));
    expect(result.current[0]).toBe("a");
    act(() => result.current[1]("c"));
    expect(result.current[0]).toBe("c");
  });
});
