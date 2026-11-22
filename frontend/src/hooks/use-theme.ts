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
  setPreference: (preference: ThemePreference) => void;
}

/** The colour-theme preference and what it currently resolves to; shared across all consumers. */
export function useTheme(): UseTheme {
  const snapshot = React.useSyncExternalStore(subscribeTheme, getThemeSnapshot, getThemeSnapshot);
  const [preference, resolved] = snapshot.split(":") as [ThemePreference, ResolvedTheme];
  return { preference, resolved, setPreference: setThemePreference };
}
