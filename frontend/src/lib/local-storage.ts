/**
 * `localStorage` access that carries a value over from the key the app used before it was renamed
 * (Spend Analyzer -> Personal Finance Analyzer; the old keys were prefixed `spend-analyzer:`).
 *
 * Every accessor swallows storage errors (private browsing, blocked storage): these are per-viewer
 * conveniences, never something the screen needs in order to render.
 */

/**
 * The value stored under `key`. If `key` is empty but `legacyKey` holds a value, that value is
 * returned, written under `key` and removed from `legacyKey` (so each browser migrates once).
 * Returns `null` when neither has a value or storage is unavailable.
 */
export function readWithLegacy(key: string, legacyKey: string): string | null {
  try {
    const current = window.localStorage.getItem(key);
    if (current !== null) return current;
    const legacy = window.localStorage.getItem(legacyKey);
    if (legacy === null) return null;
    try {
      window.localStorage.setItem(key, legacy);
      window.localStorage.removeItem(legacyKey);
    } catch {
      // Could not move it (quota, blocked writes): the old value is still the right answer.
    }
    return legacy;
  } catch {
    return null;
  }
}
