import { describe, expect, it } from "vitest";

import { formatAmountInput, parseAmountInput } from "./amount-input";

describe("parseAmountInput", () => {
  it.each([
    ["12", 1200],
    ["12.3", 1230],
    ["12.30", 1230],
    ["1,234.50", 123450],
    [".5", 50],
    [" 0.01 ", 1],
    ["1000000000.00", 100_000_000_000],
  ])("accepts %s", (text, minor) => {
    expect(parseAmountInput(text)).toBe(minor);
  });

  it.each(["", " ", "0", "0.00", "-5", "+5", "abc", "1.234", "1,23", "1.2.3", "1000000000.01", "1e3", "."])(
    "rejects %j",
    (text) => {
      expect(parseAmountInput(text)).toBeNull();
    },
  );
});

describe("formatAmountInput", () => {
  it("formats the magnitude with two decimals", () => {
    expect(formatAmountInput(1230)).toBe("12.30");
    expect(formatAmountInput(-250000)).toBe("2500.00");
    expect(formatAmountInput(5)).toBe("0.05");
  });
});
