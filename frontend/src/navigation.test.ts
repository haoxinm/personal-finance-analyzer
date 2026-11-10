import { describe, expect, it } from "vitest";

import { LEGACY_REDIRECTS, SECTIONS, carriedSearch, routeInfo } from "@/navigation";
import { screenMeta as dashboardMeta } from "@/screens/dashboard";
import { screenMeta as importMeta } from "@/screens/import";
import { screenMeta as layoutMapperMeta } from "@/screens/layout-mapper";
import { screenMeta as netWorthMeta } from "@/screens/net-worth";
import { screenMeta as forecastMeta } from "@/screens/net-worth/forecast";
import { screenMeta as reviewMeta } from "@/screens/review";
import { screenMeta as rulesMeta } from "@/screens/rules";
import { screenMeta as settingsMeta } from "@/screens/settings";
import { screenMeta as taxDocumentsMeta } from "@/screens/tax/documents-screen";
import { screenMeta as taxEstimateMeta } from "@/screens/tax/estimate-screen";
import { screenMeta as taxImportMeta } from "@/screens/tax/import-screen";
import { screenMeta as taxOverviewMeta } from "@/screens/tax/overview-screen";
import { screenMeta as transactionsMeta } from "@/screens/transactions";

const SCREEN_PATHS = [
  dashboardMeta,
  transactionsMeta,
  reviewMeta,
  importMeta,
  rulesMeta,
  layoutMapperMeta,
  taxOverviewMeta,
  taxEstimateMeta,
  taxDocumentsMeta,
  taxImportMeta,
  netWorthMeta,
  forecastMeta,
  settingsMeta,
].map((m) => m.path);

const tabPaths = SECTIONS.flatMap((s) => s.tabs.map((t) => t.path));

describe("SECTIONS", () => {
  it("lists exactly the screens' own routes (plus the gallery)", () => {
    expect([...tabPaths].sort()).toEqual([...SCREEN_PATHS, "/gallery"].sort());
  });

  it("keeps every tab under its section's base", () => {
    for (const section of SECTIONS) {
      for (const tab of section.tabs) expect(tab.path.startsWith(section.base)).toBe(true);
    }
  });

  it("redirects every legacy path to a real tab", () => {
    for (const to of Object.values(LEGACY_REDIRECTS)) expect(tabPaths).toContain(to);
  });
});

describe("routeInfo", () => {
  it("resolves the section, tab and chrome for each route", () => {
    const cases: Array<[string, string, string, { userBar: boolean; compare: boolean; note?: string }]> = [
      ["/spending", "spending", "Overview", { userBar: true, compare: true }],
      ["/spending/transactions", "spending", "Transactions", { userBar: true, compare: true }],
      ["/spending/review", "spending", "Review", { userBar: true, compare: true }],
      ["/spending/import", "spending", "Import", { userBar: true, compare: false }],
      ["/spending/rules", "spending", "Rules", { userBar: true, compare: false, note: "Rules apply to all users." }],
      ["/spending/layout-mapper", "spending", "Layout mapper", { userBar: true, compare: false }],
      ["/tax", "tax", "Overview", { userBar: true, compare: true }],
      ["/tax/estimate", "tax", "Estimate", { userBar: true, compare: true }],
      ["/tax/documents", "tax", "Documents", { userBar: true, compare: true }],
      ["/tax/import", "tax", "Import", { userBar: true, compare: false }],
      ["/net-worth", "net-worth", "Overview", { userBar: true, compare: true }],
      ["/net-worth/forecast", "net-worth", "Forecast", { userBar: true, compare: false }],
      ["/settings", "settings", "Settings", { userBar: false, compare: false }],
      ["/gallery", "gallery", "Component gallery", { userBar: false, compare: false }],
    ];
    for (const [path, section, tab, chrome] of cases) {
      const info = routeInfo(path);
      expect([path, info.section?.key, info.tab?.label]).toEqual([path, section, tab]);
      expect([path, info.userBar, info.compare, info.note]).toEqual([path, chrome.userBar, chrome.compare, chrome.note]);
    }
  });

  it("tolerates a trailing slash and falls back for unknown paths", () => {
    expect(routeInfo("/tax/documents/").tab?.label).toBe("Documents");
    const unknown = routeInfo("/nope");
    expect(unknown).toMatchObject({ section: null, tab: null, userBar: true, compare: false, title: "Personal Finance Analyzer" });
  });

  it("builds document titles, collapsing one-tab sections", () => {
    expect(routeInfo("/spending/review").title).toBe("Review · Spending · Personal Finance Analyzer");
    expect(routeInfo("/tax").title).toBe("Overview · Income & tax · Personal Finance Analyzer");
    expect(routeInfo("/tax/estimate").title).toBe("Estimate · Income & tax · Personal Finance Analyzer");
    expect(routeInfo("/spending/layout-mapper").title).toBe("Layout mapper · Spending · Personal Finance Analyzer");
    expect(routeInfo("/net-worth").title).toBe("Overview · Net worth · Personal Finance Analyzer");
    expect(routeInfo("/net-worth/forecast").title).toBe("Forecast · Net worth · Personal Finance Analyzer");
    expect(routeInfo("/settings").title).toBe("Settings · Personal Finance Analyzer");
  });
});

describe("carriedSearch", () => {
  const tab = (path: string) => SECTIONS.flatMap((s) => s.tabs).find((t) => t.path === path)!;

  it("carries compare only to compare-capable tabs", () => {
    expect(carriedSearch(tab("/spending/review"), "?user=1&compare=2", true)).toBe("?compare=2");
    expect(carriedSearch(tab("/spending/rules"), "?user=1&compare=2", true)).toBe("");
    expect(carriedSearch(tab("/net-worth"), "?compare=2", true)).toBe("?compare=2");
    expect(carriedSearch(tab("/net-worth/forecast"), "?compare=2", true)).toBe("");
  });

  it("carries keepParams only within the same section", () => {
    expect(carriedSearch(tab("/tax/documents"), "?year=2023", true)).toBe("?year=2023");
    expect(carriedSearch(tab("/tax/estimate"), "?year=2023&compare=2", true)).toBe("?compare=2&year=2023");
    expect(carriedSearch(tab("/tax/documents"), "?year=2023", false)).toBe("");
    expect(carriedSearch(tab("/tax/documents"), "?year=2023&compare=2&doc=4", true)).toBe("?compare=2&year=2023");
  });
});
