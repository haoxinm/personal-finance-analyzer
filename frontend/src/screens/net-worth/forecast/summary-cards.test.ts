import { describe, expect, it } from "vitest";

import { growthBreakdown } from "./params";

describe("growthBreakdown", () => {
  it("gives the growth share when saving and growth both add", () => {
    expect(growthBreakdown(3_000_000, 1_000_000)).toBe("25% of the total change");
    expect(growthBreakdown(0, 500)).toBe("100% of the total change");
  });

  it("gives the share when both subtract", () => {
    expect(growthBreakdown(-3_000_000, -1_000_000)).toBe("25% of the total change");
  });

  it("never reads as an absurd percentage when they have opposite signs", () => {
    // A drawdown of 100,000 with 90,000 of growth: the net change is -10,000 and the share would
    // read 900%. Say what offsets what instead.
    expect(growthBreakdown(-10_000_000, 9_000_000)).toBe("Offsets $100,000 of net withdrawals over the forecast");
    expect(growthBreakdown(5_000_000, -4_500_000)).toBe("Losses offset $45,000 of the $50,000 saved");
  });

  it("falls back when there is no change", () => {
    expect(growthBreakdown(0, 0)).toBe("Total over the forecast");
  });
});
