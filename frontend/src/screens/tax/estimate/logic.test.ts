import { describe, expect, it } from "vitest";

import { estimateCalc, taxProfile } from "./test-fixtures";
import {
  AGE_LIMITS,
  ageLimits,
  breakdownSections,
  clampAge,
  defaultEstimateYear,
  estimateYearOptions,
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
    expect(d).toMatchObject({
      filingStatus: "mfj",
      spouseId: 2,
      deductionMode: "standard",
      age65OrBlind: 0,
      age65: 0,
    });
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

describe("age counts", () => {
  it("mirrors the API's limits: boxes 2 and aged 65 or older 1, or 4 and 2 when joint", () => {
    expect(AGE_LIMITS).toEqual({ person: { boxes: 2, age65: 1 }, joint: { boxes: 4, age65: 2 } });
    expect(ageLimits("mfj")).toEqual({ boxes: 4, age65: 2 });
    for (const status of ["single", "mfs", "hoh", "qss"] as const) {
      expect(ageLimits(status)).toEqual({ boxes: 2, age65: 1 });
    }
  });

  it("reads the saved count and sends it in the body", () => {
    const d = draftFromProfile(taxProfile({ age65_or_blind_count: 2, age65_count: 1, filing_status: "mfj" }), FALLBACK);
    expect(d).toMatchObject({ age65OrBlind: 2, age65: 1 });
    const result = draftToBody(d);
    expect("body" in result && result.body).toMatchObject({ age65_or_blind_count: 2, age65_count: 1 });
  });

  it("keeps a joint profile's household total of 4 boxes and 2 people aged 65 or older", () => {
    const d = draftFromProfile(taxProfile({ age65_or_blind_count: 4, age65_count: 2, filing_status: "mfj" }), FALLBACK);
    expect(d).toMatchObject({ age65OrBlind: 4, age65: 2 });
    const result = draftToBody(d);
    expect("body" in result && result.body).toMatchObject({ age65_or_blind_count: 4, age65_count: 2 });
  });

  it("brings a profile saved with more than its status allows back within the limits", () => {
    const single = draftFromProfile(taxProfile({ age65_or_blind_count: 4, age65_count: 2, filing_status: "single" }), FALLBACK);
    expect(single).toMatchObject({ age65OrBlind: 2, age65: 1 });
    const joint = draftFromProfile(taxProfile({ age65_or_blind_count: 4, age65_count: 3, filing_status: "mfj" }), FALLBACK);
    expect(joint).toMatchObject({ age65OrBlind: 4, age65: 2 });
  });

  it("clamps a draft that exceeds the limits, and leaves a valid one alone", () => {
    const joint = { ...draftFromProfile(null, FALLBACK), filingStatus: "mfj" as const, age65OrBlind: 4, age65: 2 };
    expect(clampAge(joint)).toBe(joint);
    // Leaving mfj for another status brings the counts back to one person's.
    expect(clampAge({ ...joint, filingStatus: "single" })).toMatchObject({ age65OrBlind: 2, age65: 1 });
    expect(clampAge({ ...joint, filingStatus: "hoh", age65OrBlind: 3, age65: 1 })).toMatchObject({ age65OrBlind: 2, age65: 1 });
    // The "65 or older" count is a subset of the boxes.
    expect(clampAge({ ...joint, age65OrBlind: 1 })).toMatchObject({ age65OrBlind: 1, age65: 1 });
    expect(clampAge({ ...joint, age65OrBlind: 0 })).toMatchObject({ age65OrBlind: 0, age65: 0 });
  });

  it("refuses counts the API would refuse: 2 and 1 per person, 4 and 2 when joint", () => {
    for (const filingStatus of ["single", "mfs", "hoh", "qss"] as const) {
      const base = { ...draftFromProfile(null, FALLBACK), filingStatus };
      const tooMany = draftToBody({ ...base, age65OrBlind: 3 });
      expect("errors" in tooMany && tooMany.errors.age65OrBlind).toMatch(/At most 2 per person/);
      const tooManySeniors = draftToBody({ ...base, age65OrBlind: 2, age65: 2 });
      expect("errors" in tooManySeniors && tooManySeniors.errors.age65).toMatch(/At most 1 per person/);
      const notSubset = draftToBody({ ...base, age65OrBlind: 0, age65: 1 });
      expect("errors" in notSubset && notSubset.errors.age65).toMatch(/subset/);
      expect("body" in draftToBody({ ...base, age65OrBlind: 2, age65: 1 })).toBe(true);
    }
    const joint = { ...draftFromProfile(null, FALLBACK), filingStatus: "mfj" as const };
    const tooMany = draftToBody({ ...joint, age65OrBlind: 5 });
    expect("errors" in tooMany && tooMany.errors.age65OrBlind).toMatch(/At most 4 for a joint profile/);
    const tooManySeniors = draftToBody({ ...joint, age65OrBlind: 4, age65: 3 });
    expect("errors" in tooManySeniors && tooManySeniors.errors.age65).toMatch(/At most 2 for a joint profile/);
    const notSubset = draftToBody({ ...joint, age65OrBlind: 1, age65: 2 });
    expect("errors" in notSubset && notSubset.errors.age65).toMatch(/subset/);
    expect("body" in draftToBody({ ...joint, age65OrBlind: 3, age65: 2 })).toBe(true);
    expect("body" in draftToBody({ ...joint, age65OrBlind: 4, age65: 2 })).toBe(true);
  });

  it("is part of what makes two drafts the same", () => {
    const a = draftFromProfile(null, FALLBACK);
    expect(sameDraft({ ...a, age65OrBlind: 1, age65: 1 }, { ...a, age65OrBlind: 1, age65: 0 })).toBe(false);
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

  it("shows the senior deduction, NIIT, Additional Medicare Tax and the payments that are not withholding", () => {
    // One single senior at the fixture's AGI of $103,140: $6,000 less 6% of the $28,140 over $75,000.
    const calc = estimateCalc({
      senior_deduction_minor: 431160,
      nonrefundable_credits_minor: 100,
      nonrefundable_credits_entered_minor: 500,
      net_investment_income_tax_minor: 76000,
      additional_medicare_tax_minor: 90000,
      refundable_credits_minor: 300000,
      additional_medicare_withheld_minor: 45000,
      excess_social_security_credit_minor: 20000,
    });
    const sections = breakdownSections(calc);
    const keysOf = (section: string) => sections.find((s) => s.key === section)!.lines.map((l) => l.key);
    expect(keysOf("taxable")).toContain("senior-deduction");
    expect(keysOf("tax")).toEqual(
      expect.arrayContaining(["nonrefundable", "nonrefundable-entered", "niit", "additional-medicare"]),
    );
    // Refundable credits are payments (never a subtraction in the tax section).
    expect(keysOf("tax")).not.toContain("refundable");
    expect(keysOf("payments")).toEqual([
      "withheld",
      "refundable",
      "additional-medicare-withheld",
      "excess-ss",
      "payments",
    ]);
    const lines = sections.flatMap((s) => s.lines);
    const by = (k: string) => lines.find((l) => l.key === k)!;
    expect(by("senior-deduction")).toMatchObject({ minus: true, minor: 431160 });
    expect(by("nonrefundable")).toMatchObject({ minus: true, minor: 100 });
    expect(by("nonrefundable-entered")).toMatchObject({ kind: "memo", minor: 500 });
    expect(by("refundable").minus).toBeUndefined();
    expect(by("excess-ss")).toMatchObject({ source: "computed", minor: 20000 });
    expect(by("additional-medicare-withheld")).toMatchObject({ source: "documents", minor: 45000 });
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

describe("estimate year options", () => {
  const now = new Date("2026-10-30T12:00:00Z");

  it("adds the current and previous year to the years with forms, newest first, without duplicates", () => {
    expect(estimateYearOptions([], now)).toEqual([2026, 2025]);
    expect(estimateYearOptions([2024, 2023], now)).toEqual([2026, 2025, 2024, 2023]);
    expect(estimateYearOptions([2025, 2022], now)).toEqual([2026, 2025, 2022]);
  });

  it("defaults to the newest year with forms, else the previous calendar year", () => {
    expect(defaultEstimateYear([2024, 2023], now)).toBe(2024);
    expect(defaultEstimateYear([2023, 2024], now)).toBe(2024);
    expect(defaultEstimateYear([], now)).toBe(2025);
  });
});
