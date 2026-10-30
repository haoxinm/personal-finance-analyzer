import { matchPath } from "react-router-dom";

import { COMPARE_USER_PARAM } from "@/hooks/use-selected-user";

export const APP_NAME = "Spend Analyzer";

export interface TabSpec {
  /** Absolute route path. */
  path: string;
  label: string;
  /** NavLink `end`: the index tab of a section must not stay active on its siblings. */
  end?: boolean;
  /** Can be rendered twice (left/right) in compare mode. */
  compare?: boolean;
  /** No per-user view at all: the global user bar is hidden. */
  noUserBar?: boolean;
  /** Clarification shown next to the user bar. */
  note?: string;
  /** Search params carried over when moving between tabs of the same section (e.g. `year`). */
  keepParams?: readonly string[];
  /** Routable but not listed as a sub-tab (deep-link-only screens). */
  hidden?: boolean;
}

export interface SectionSpec {
  key: string;
  label: string;
  /** Every route of the section lives under this path. */
  base: string;
  tabs: readonly TabSpec[];
  /** Left out of the top nav (developer tools). */
  hidden?: boolean;
  /** Pushed to the right edge of the top nav. */
  pushRight?: boolean;
}

/**
 * The app's information architecture and the single source of truth for the shell: the top nav,
 * each section's sub-tabs, and per-route chrome (user bar, compare, notes, document title). Screens
 * own their component and `screenMeta.path`; `navigation.test.ts` checks every one is listed here.
 */
export const SECTIONS: readonly SectionSpec[] = [
  {
    key: "spending",
    label: "Spending",
    base: "/spending",
    tabs: [
      { path: "/spending", label: "Overview", end: true, compare: true },
      { path: "/spending/transactions", label: "Transactions", compare: true },
      { path: "/spending/review", label: "Review", compare: true },
      { path: "/spending/import", label: "Import" },
      { path: "/spending/rules", label: "Rules", note: "Rules apply to all users." },
      // Reached from Import's `unsupported_layout` failure and Rules' Extractors tab.
      { path: "/spending/layout-mapper", label: "Layout mapper", hidden: true },
    ],
  },
  {
    key: "tax",
    label: "Income & tax",
    base: "/tax",
    tabs: [
      { path: "/tax", label: "Overview", end: true, compare: true, keepParams: ["year"] },
      { path: "/tax/estimate", label: "Estimate", compare: true, keepParams: ["year"] },
      { path: "/tax/documents", label: "Documents", compare: true, keepParams: ["year"] },
      { path: "/tax/import", label: "Import", keepParams: ["year"] },
    ],
  },
  {
    key: "net-worth",
    label: "Net worth",
    base: "/net-worth",
    tabs: [
      { path: "/net-worth", label: "Overview", end: true, compare: true },
      // A parameter-heavy planning screen: one person (or everyone) at a time, no compare.
      { path: "/net-worth/forecast", label: "Forecast" },
    ],
  },
  {
    key: "settings",
    label: "Settings",
    base: "/settings",
    pushRight: true,
    tabs: [{ path: "/settings", label: "Settings", end: true, noUserBar: true }],
  },
  {
    // A developer tool: routable, but never part of the nav.
    key: "gallery",
    label: "Component gallery",
    base: "/gallery",
    hidden: true,
    tabs: [{ path: "/gallery", label: "Component gallery", end: true, noUserBar: true }],
  },
];

/** Pre-restructure paths, redirected (keeping the query string) to where each screen lives now. */
export const LEGACY_REDIRECTS: Readonly<Record<string, string>> = {
  "/dashboard": "/spending",
  "/import": "/spending/import",
  "/review": "/spending/review",
  "/transactions": "/spending/transactions",
  "/rules": "/spending/rules",
  "/layout-mapper": "/spending/layout-mapper",
};

/** The page `/` lands on. */
export const HOME_PATH = "/spending";

export interface RouteInfo {
  section: SectionSpec | null;
  tab: TabSpec | null;
  /** Show the global user bar. */
  userBar: boolean;
  /** Compare mode can split this route into two panes. */
  compare: boolean;
  note?: string;
  /** `document.title` for the route. */
  title: string;
}

/** Resolves a pathname to its section and tab; an unknown path yields no section and the defaults. */
export function routeInfo(pathname: string): RouteInfo {
  const section = SECTIONS.find((s) => matchPath({ path: s.base, end: false }, pathname)) ?? null;
  const tab = section?.tabs.find((t) => matchPath({ path: t.path, end: true }, pathname)) ?? null;
  return {
    section,
    tab,
    userBar: !tab?.noUserBar,
    compare: tab?.compare ?? false,
    note: tab?.note,
    title: documentTitle(section, tab),
  };
}

function documentTitle(section: SectionSpec | null, tab: TabSpec | null): string {
  if (!section || !tab) return APP_NAME;
  // A one-tab section's tab repeats the section name ("Settings · Settings").
  const parts = section.tabs.length === 1 || tab.label === section.label ? [section.label] : [tab.label, section.label];
  return [...parts, APP_NAME].join(" · ");
}

/** Labels of the sections with at least one compare-capable tab, for the user bar's explanation. */
export function compareSectionLabels(): string[] {
  return SECTIONS.filter((s) => !s.hidden && s.tabs.some((t) => t.compare)).map((s) => s.label);
}

/** The tabs a section lists in its second nav row. */
export function visibleTabs(section: SectionSpec): TabSpec[] {
  return section.tabs.filter((t) => !t.hidden);
}

/**
 * The query string a nav link to `tab` carries over from the current one: an active
 * `?compare=` onto compare-capable tabs, and the tab's `keepParams` when staying in the same section.
 */
export function carriedSearch(tab: TabSpec, currentSearch: string, sameSection: boolean): string {
  const current = new URLSearchParams(currentSearch);
  const next = new URLSearchParams();
  const compare = current.get(COMPARE_USER_PARAM);
  if (compare && tab.compare) next.set(COMPARE_USER_PARAM, compare);
  if (sameSection) {
    for (const key of tab.keepParams ?? []) {
      const value = current.get(key);
      if (value !== null) next.set(key, value);
    }
  }
  const qs = next.toString();
  return qs ? `?${qs}` : "";
}
