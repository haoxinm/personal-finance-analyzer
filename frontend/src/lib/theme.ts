/**
 * Colour theme: the preference ("light" | "dark" | "system"), how it resolves, and applying it
 * to <html>. Tailwind's `dark:` variants need the `dark` class; `color-scheme` makes native
 * controls and scrollbars match. "system" is stored by removing the key.
 *
 * The inline script in index.html applies the stored theme before first paint; `initTheme()`
 * re-applies it (idempotent) and follows OS changes while the preference is "system".
 */
export type ThemePreference = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "pfa:theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

/** The last choice made in this page view; used when storage is blocked so it is not forgotten. */
let memoryPreference: ThemePreference | null = null;

export function readThemePreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return stored === "light" || stored === "dark" ? stored : "system";
  } catch {
    return memoryPreference ?? "system";
  }
}

export function writeThemePreference(preference: ThemePreference): void {
  memoryPreference = preference;
  try {
    if (preference === "system") window.localStorage.removeItem(THEME_STORAGE_KEY);
    else window.localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    // Blocked storage: the choice still applies for this page view.
  }
}

export function systemPrefersDark(): boolean {
  try {
    return typeof window.matchMedia === "function" && window.matchMedia(DARK_QUERY).matches;
  } catch {
    return false;
  }
}

export function resolveTheme(preference: ThemePreference, systemDark: boolean): ResolvedTheme {
  if (preference === "system") return systemDark ? "dark" : "light";
  return preference;
}

export function applyTheme(resolved: ResolvedTheme, preference: ThemePreference): void {
  const root = document.documentElement;
  root.classList.toggle("dark", resolved === "dark");
  root.style.colorScheme = resolved;
  root.setAttribute("data-theme-preference", preference);
}

// ---- external store (useSyncExternalStore) -------------------------------------------------

let preferenceState: ThemePreference = "system";
let resolvedState: ResolvedTheme = "light";
// A primitive string snapshot: stable between changes, so React does not re-render needlessly.
let systemDarkState = false;
let snapshot = `${preferenceState}:${resolvedState}:${systemDarkState}`;
const listeners = new Set<() => void>();

function emit(): void {
  snapshot = `${preferenceState}:${resolvedState}:${systemDarkState}`;
  listeners.forEach((l) => l());
}

/** Re-read preference + OS setting, apply to <html>, and notify subscribers. */
function sync(): void {
  preferenceState = readThemePreference();
  systemDarkState = systemPrefersDark();
  resolvedState = resolveTheme(preferenceState, systemDarkState);
  applyTheme(resolvedState, preferenceState);
  emit();
}

let initialised = false;

/** Apply the stored theme and start following OS / other-tab changes. Safe to call repeatedly. */
export function initTheme(): void {
  sync();
  if (initialised) return;
  initialised = true;
  try {
    if (typeof window.matchMedia === "function") {
      const mql = window.matchMedia(DARK_QUERY);
      mql.addEventListener("change", sync);
    }
  } catch {
    // No matchMedia: "system" stays on whatever it resolved to.
  }
  window.addEventListener("storage", (event) => {
    if (event.key === null || event.key === THEME_STORAGE_KEY) sync();
  });
}

export function setThemePreference(preference: ThemePreference): void {
  writeThemePreference(preference);
  // Apply what the user chose even if storage refused the write.
  preferenceState = preference;
  systemDarkState = systemPrefersDark();
  resolvedState = resolveTheme(preference, systemDarkState);
  applyTheme(resolvedState, preference);
  emit();
}

export function subscribeTheme(callback: () => void): () => void {
  listeners.add(callback);
  return () => listeners.delete(callback);
}

export function getThemeSnapshot(): string {
  return snapshot;
}
