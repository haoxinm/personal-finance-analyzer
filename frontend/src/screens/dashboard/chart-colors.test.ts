import { describe, expect, it } from "vitest";

import { CHART_INFLOW, CHART_OUTFLOW, colorByIndex, colorMapForKeys } from "./chart-colors";

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
