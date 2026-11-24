import { describe, expect, it } from "vitest";

import { CHART_INFLOW, CHART_OUTFLOW, colorByIndex, colorMapForKeys, stableColorMap } from "./chart-colors";

describe("colorMapForKeys", () => {
  it("gives every visible series a distinct colour", () => {
    const keys = ["cash", "others", "groceries", "dining", "rent", "travel", "fees", "health", "utilities"];
    const map = colorMapForKeys(keys);
    expect(new Set(map.values()).size).toBe(keys.length);
    expect(map.get("cash")).not.toBe(map.get("others"));
  });
  it("is independent of input order", () => {
    const a = colorMapForKeys(["cash", "others", "rent"]);
    const b = colorMapForKeys(["rent", "others", "cash"]);
    expect([...a.entries()].sort()).toEqual([...b.entries()].sort());
  });
  it("uses theme tokens, not literal colours", () => {
    for (const colour of [...colorMapForKeys(["a", "b", "c"]).values(), CHART_OUTFLOW, CHART_INFLOW]) {
      expect(colour).toMatch(/^hsl\(var\(--[a-z-]+\)\)$/);
    }
    expect(colorByIndex(0)).toBe("hsl(var(--chart-rose))");
  });
});

describe("colorMapForKeys avoidNeutral", () => {
  it("never hands out the neutral slate used for Other", () => {
    const keys = Array.from({ length: 14 }, (_, i) => `k${String(i).padStart(2, "0")}`);
    const map = colorMapForKeys(keys, true);
    expect([...map.values()]).not.toContain("hsl(var(--chart-slate))");
    expect(new Set([...map.values()]).size).toBe(11);
  });
});

describe("stableColorMap", () => {
  const taxonomy = ["cash", "dining", "groceries", "health", "others", "rent"];
  it("keeps a category's colour whatever else is displayed", () => {
    const wide = stableColorMap(["dining", "groceries", "rent"], taxonomy);
    const narrow = stableColorMap(["groceries", "rent"], taxonomy);
    expect(narrow.get("groceries")).toBe(wide.get("groceries"));
    expect(narrow.get("rent")).toBe(wide.get("rent"));
  });
  it("keeps displayed series distinct even when the taxonomy outgrows the palette", () => {
    const order = Array.from({ length: 15 }, (_, i) => `c${String(i).padStart(2, "0")}`);
    const map = stableColorMap(order, order);
    expect(new Set(map.values()).size).toBe(11);
    expect(new Set(stableColorMap(["c00", "c11"], order).values()).size).toBe(2);
  });
  it("handles keys outside the taxonomy and never uses the neutral slate", () => {
    const map = stableColorMap(["uncategorized", "cash"], taxonomy);
    expect(map.size).toBe(2);
    expect([...map.values()]).not.toContain("hsl(var(--chart-slate))");
  });
});
