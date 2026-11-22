import * as React from "react";

/**
 * A per-browser viewing preference (e.g. which chart type a card shows) kept in `localStorage`.
 *
 * Built on `useSyncExternalStore` over a module-level listener registry plus the `storage` event,
 * so every component using the same key (both panes in compare mode) switches together and other
 * tabs follow. Every storage access is wrapped in try/catch; when storage is unavailable the
 * choice still works for this page load via an in-memory copy. A stored value outside `allowed`
 * falls back to `fallback`.
 */
const listeners = new Map<string, Set<() => void>>();
const memory = new Map<string, string>();

function notify(key: string): void {
  listeners.get(key)?.forEach((listener) => listener());
}

function readRaw(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return memory.get(key) ?? null;
  }
}

function subscribeTo(key: string, callback: () => void): () => void {
  let set = listeners.get(key);
  if (!set) {
    set = new Set();
    listeners.set(key, set);
  }
  set.add(callback);
  const onStorage = (event: StorageEvent) => {
    if (event.key === key || event.key === null) callback();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    set.delete(callback);
    window.removeEventListener("storage", onStorage);
  };
}

export function usePersistentChoice<T extends string>(
  key: string,
  allowed: readonly T[],
  fallback: T,
): [T, (value: T) => void] {
  const subscribe = React.useCallback((cb: () => void) => subscribeTo(key, cb), [key]);
  const getSnapshot = React.useCallback(() => readRaw(key), [key]);
  const raw = React.useSyncExternalStore(subscribe, getSnapshot, () => null);

  const value = (allowed as readonly string[]).includes(raw ?? "") ? (raw as T) : fallback;

  const setValue = React.useCallback(
    (next: T) => {
      memory.set(key, next);
      try {
        window.localStorage.setItem(key, next);
      } catch {
        // Blocked or full storage: the in-memory copy keeps this page load consistent.
      }
      notify(key);
    },
    [key],
  );

  return [value, setValue];
}
