import * as React from "react";

import {
  type ResolvedTheme,
  type ThemePreference,
  getThemeSnapshot,
  setThemePreference,
  subscribeTheme,
} from "@/lib/theme";

export interface UseTheme {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  /** What the OS currently prefers, independent of the chosen preference. */
  systemDark: boolean;
  setPreference: (preference: ThemePreference) => void;
}

/** The colour-theme preference and what it currently resolves to; shared across all consumers. */
export function useTheme(): UseTheme {
  const snapshot = React.useSyncExternalStore(subscribeTheme, getThemeSnapshot, getThemeSnapshot);
  const [preference, resolved, system] = snapshot.split(":") as [ThemePreference, ResolvedTheme, string];
  return { preference, resolved, systemDark: system === "true", setPreference: setThemePreference };
}
