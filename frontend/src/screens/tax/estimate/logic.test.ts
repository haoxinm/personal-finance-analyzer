import { describe, expect, it } from "vitest";

import { estimateCalc, taxProfile } from "./test-fixtures";
import {
  breakdownSections,
  draftFromProfile,
  draftToBody,
  filingStatusLabel,
  MONEY_FIELDS,
  parseMoneyField,
  resultKind,
  resultWord,
  sameDraft,
  withUserNames,
} from "./logic";

const FALLBACK = { filingStatus: "single", spouseId: null } as const;

describe("credit help text", () => {
  const help = (key: string) => MONEY_FIELDS.find((f) => f.key === key)!.help;
  it("sends the refundable part of a credit to the refundable field", () => {
    expect(help("nonrefundable_credits_minor")).toMatch(/CTC\/ODC/);
    expect(help("nonrefundable_credits_minor")).toMatch(/refundable part \(ACTC, EITC, refundable AOTC\)/);
    expect(help("refundable_credits_minor")).toMatch(/^Refundable credits \(treated like payments\)/);
    expect(help("refundable_credits_minor")).toMatch(/ACTC/);
  });
});

describe("result wording", () => {
  it("is a refund when payments exceed tax, owe when they fall short", () => {
    expect([500, -500, 0].map(resultKind)).toEqual(["refund", "owe", "even"]);
    expect((["refund", "owe", "even"] as const).map(resultWord)).toEqual(["Refund", "Owe", "Even"]);
  });
});

describe("withUserNames / filingStatusLabel", () => {
  const users = [
    { id: 1, name: "Alex", is_default: true },
    { id: 2, name: "Sam", is_default: false },
  ];
  it("replaces user ids by names and leaves unknown ids alone", () => {
    expect(withUserNames("The profile of user 2 does not match; User 1's too; user 9 unknown", users)).toBe(
      "The profile of Sam does not match; Alex's too; user 9 unknown",
    );
  });
  it("labels filing statuses", () => {
    expect(filingStatusLabel("mfj")).toBe("Married filing jointly");
    expect(filingStatusLabel("weird")).toBe("weird");
    expect(filingStatusLabel(null)).toBe("unknown");
  });
});

describe("money fields", () => {
  it("parses dollars to cents, blank as zero, and rejects the rest", () => {
    expect(parseMoneyField("")).toBe(0);
    expect(parseMoneyField("  ")).toBe(0);
    expect(parseMoneyField("$1,234.50")).toBe(123450);
    expect(parseMoneyField("0.99")).toBe(99);
    expect(parseMoneyField("-5")).toBeNull();
    expect(parseMoneyField("abc")).toBeNull();
  });
});

describe("profile draft", () => {
  it("starts from the estimate's assumption without a profile", () => {
    const d = draftFromProfile(null, { filingStatus: "mfj", spouseId: 2 });
    expect(d).toMatchObject({ filingStatus: "mfj", spouseId: 2, deductionMode: "standard", age65OrBlind: 0 });
    expect(d.money.adjustments_minor).toBe("");
  });

  it("starts from the saved profile when there is one, ignoring the assumption", () => {
    const d = draftFromProfile(
      taxProfile({ filing_status: "hoh", adjustments_minor: 150000, itemized_deductions_minor: 1 }),
      { filingStatus: "mfj", spouseId: 2 },
    );
    expect(d).toMatchObject({ filingStatus: "hoh", spouseId: null, itemized: "0.01" });
    expect(d.money.adjustments_minor).toBe("1500.00");
  });

  it("builds the PUT body in cents and drops the spouse unless married filing jointly", () => {
    const d = draftFromProfile(null, { filingStatus: "mfj", spouseId: 2 });
    d.money.estimated_payments_minor = "4,000";
    expect(draftToBody(d)).toMatchObject({
      body: { filing_status: "mfj", joint_with_user_id: 2, estimated_payments_minor: 400000 },
    });
    expect(draftToBody({ ...d, filingStatus: "single" })).toMatchObject({
      body: { filing_status: "single", joint_with_user_id: null },
    });
  });

  it("reports each unreadable field", () => {
    const d = draftFromProfile(null, FALLBACK);
    d.itemized = "x";
    d.money.other_taxes_minor = "-1";
    expect(draftToBody(d)).toEqual({
      errors: {
        itemized: "Enter a dollar amount of 0 or more",
        other_taxes_minor: "Enter a dollar amount of 0 or more",
      },
    });
  });

  it("treats equal amounts written differently as the same draft", () => {
    const a = draftFromProfile(null, FALLBACK);
    const b = draftFromProfile(null, FALLBACK);
    b.money.adjustments_minor = "0";
    expect(sameDraft(a, b)).toBe(true);
    b.money.adjustments_minor = "1";
    expect(sameDraft(a, b)).toBe(false);
    // The spouse only matters when married filing jointly.
    expect(sameDraft({ ...a, spouseId: 2 }, a)).toBe(true);
    expect(sameDraft({ ...a, filingStatus: "mfj", spouseId: 2 }, { ...a, filingStatus: "mfj" })).toBe(false);
  });
});

describe("breakdownSections", () => {
  const keys = (calc = estimateCalc()) =>
    breakdownSections(calc).flatMap((s) => s.lines.map((l) => l.key));

  it("keeps the lines that always matter and drops zero optional ones", () => {
    expect(keys()).toEqual([
      "wages",
      "interest",
      "dividends",
      "qualified",
      "gross",
      "agi",
      "deduction",
      "taxable",
      "preferential",
      "ordinary-tax",
      "preferential-tax",
      "income-tax",
      "total-tax",
      "withheld",
      "payments",
    ]);
  });

  it("adds the capital gains lines when any gain, loss or carryover is involved", () => {
    const k = keys(
      estimateCalc({
        short_term_gain_minor: -100000,
        capital_loss_carryover_in_minor: 50000,
        capital_result_counted_minor: -150000,
      }),
    );
    expect(k).toEqual(expect.arrayContaining(["capital", "stg", "carry-in"]));
    expect(k).not.toContain("ltg");
  });

  it("attributes inputs and marks subtractions", () => {
    const lines = breakdownSections(
      estimateCalc({ adjustments_minor: 1, nonrefundable_credits_minor: 2, profile_other_income_minor: 3 }),
    ).flatMap((s) => s.lines);
    const by = (k: string) => lines.find((l) => l.key === k)!;
    expect(by("wages")).toMatchObject({ source: "documents", minus: undefined });
    expect(by("adjustments")).toMatchObject({ source: "profile", minus: true });
    expect(by("nonrefundable")).toMatchObject({ source: "profile", minus: true });
    expect(by("other-profile")).toMatchObject({ source: "profile" });
    expect(by("deduction")).toMatchObject({ source: "computed", minus: true });
    expect(by("total-tax").kind).toBe("total");
  });
});
