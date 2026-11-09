import { describe, expect, it } from "vitest";

import { checkOutcomeLabel, coveredYearsLabel, lastCheckedLabel, parameterSourceLabel } from "./tax-params";

describe("coveredYearsLabel", () => {
  it("collapses runs of consecutive years", () => {
    expect(coveredYearsLabel([2022, 2023, 2024, 2025, 2026])).toBe("2022-2026");
    expect(coveredYearsLabel([2026, 2022, 2023, 2025])).toBe("2022-2023, 2025-2026");
    expect(coveredYearsLabel([2024])).toBe("2024");
    expect(coveredYearsLabel([2022, 2024, 2024])).toBe("2022, 2024");
    expect(coveredYearsLabel([])).toBe("none");
  });
});

describe("labels", () => {
  it("formats the last check date without a timezone shift, and 'Never' without one", () => {
    expect(lastCheckedLabel("2026-03-01")).toBe("Mar 1, 2026");
    expect(lastCheckedLabel(null)).toBe("Never");
    expect(lastCheckedLabel("garbage")).toBe("garbage");
  });
  it("names the parameter source", () => {
    expect(["bundled", "cached", "override"].map((s) => parameterSourceLabel(s as "bundled"))).toEqual([
      "Bundled with the app",
      "Downloaded update",
      "Your own file",
    ]);
  });
});

describe("checkOutcomeLabel", () => {
  it("maps every stored outcome to readable text", () => {
    expect(["updated", "current", "rejected", "unreachable", "pending"].map(checkOutcomeLabel)).toEqual([
      "Updated to newer figures",
      "Already up to date",
      "Download rejected (failed validation)",
      "Could not reach GitHub",
      "Check started, not finished",
    ]);
    expect(checkOutcomeLabel(null)).toBe("—");
    expect(checkOutcomeLabel("weird")).toBe("weird");
  });
});
