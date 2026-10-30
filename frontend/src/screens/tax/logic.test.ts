import { describe, expect, it } from "vitest";

import {
  basisNotice,
  formatRate,
  formTypeLabel,
  groupPayers,
  headlineBasisLabel,
  headlineBasisNote,
  headlineTax,
  incomeBasisLabel,
  refundOrOwed,
  resolveYear,
  supersededByDocument,
  taxBreakdownRows,
  donutSlices,
  incomeShare,
  toIncomeBars,
  yearOverYear,
  yearPointLabel,
} from "./logic";
import { PAYERS, summary } from "./test-fixtures";

describe("formatRate", () => {
  it("formats fractions as percentages and null as a dash", () => {
    expect(formatRate(0.1319)).toBe("13.19%");
    expect(formatRate(0)).toBe("0.00%");
    expect(formatRate(null)).toBe("—");
  });
});

describe("labels", () => {
  it("labels bases and forms", () => {
    expect(incomeBasisLabel("mixed")).toMatch(/mixed/i);
    expect(basisNotice("source_documents")).toBeNull();
    expect(basisNotice("return")).toBeNull();
    expect(basisNotice("mixed")).toMatch(/1040/);
    expect(formTypeLabel("1099-div")).toBe("1099-DIV");
    expect(formTypeLabel("weird")).toBe("weird");
  });
});

describe("toIncomeBars", () => {
  it("drops memo and zero categories", () => {
    const bars = toIncomeBars(summary());
    expect(bars.map((b) => b.category)).toEqual([
      "salary_wages",
      "interest",
      "ordinary_dividends",
    ]);
    expect(bars.reduce((s, b) => s + b.value, 0)).toBe(
      10974260 + 24000 + 290000,
    );
  });
});

describe("headlineTax", () => {
  it("maps the server's headline without recomputing it", () => {
    const t = headlineTax(summary());
    expect(t).toEqual({
      federalMinor: 1489100,
      federalBasis: "return",
      ficaMinor: 863244,
      stateMinor: 500000,
      totalMinor: 1489100 + 863244 + 500000,
    });
  });
  it("trusts the headline over the summary's other fields", () => {
    const t = headlineTax(
      summary({
        liability: [],
        headline: {
          federal_minor: 111,
          federal_basis: "estimate",
          fica_minor: 2,
          state_minor: 3,
          total_minor: 116,
        },
      }),
    );
    expect(t).toMatchObject({ federalMinor: 111, federalBasis: "estimate", totalMinor: 116 });
  });
  it("is null when the year has no tax figure", () => {
    expect(headlineTax(summary({ headline: null }))).toBeNull();
    expect(headlineTax(summary({ headline: undefined }))).toBeNull();
  });
  it("labels every basis", () => {
    expect(
      (["return", "estimate", "withholding", "mixed"] as const).map(headlineBasisLabel),
    ).toEqual(["from return", "estimated", "withholding only", "mixed"]);
    for (const b of ["return", "estimate", "withholding", "mixed"] as const)
      expect(headlineBasisNote(b)).not.toBe("");
  });
});

describe("supersededByDocument", () => {
  it("collects replaced forms per document across summaries", () => {
    const row = (form_id: number, document_id: number, form_type: "w2" | "1099-int") => ({
      form_id,
      document_id,
      user_id: 1,
      form_type,
      superseded_by_form_id: 99,
    });
    const map = supersededByDocument([
      summary({ superseded_forms: [row(1, 10, "w2"), row(2, 10, "1099-int")] }),
      summary({ tax_year: 2023, superseded_forms: [row(3, 11, "w2")] }),
      summary({ tax_year: 2022, superseded_forms: undefined }),
    ]);
    expect(map.get(10)).toEqual(["w2", "1099-int"]);
    expect(map.get(11)).toEqual(["w2"]);
    expect(map.has(12)).toBe(false);
  });
});

describe("refundOrOwed", () => {
  it("classifies", () => {
    expect(refundOrOwed(summary())).toMatchObject({
      kind: "refund",
      amountMinor: 30000,
    });
    expect(
      refundOrOwed(summary({ refund_minor: 0, amount_owed_minor: 500 })),
    ).toMatchObject({ kind: "owed", amountMinor: 500 });
    expect(
      refundOrOwed(summary({ refund_minor: null, amount_owed_minor: null }))
        .kind,
    ).toBe("unknown");
    expect(
      refundOrOwed(summary({ refund_minor: 0, amount_owed_minor: 0 })).kind,
    ).toBe("even");
  });
});

describe("taxBreakdownRows", () => {
  it("groups rows and flags estimates", () => {
    const groups = taxBreakdownRows(summary());
    expect(groups.map((g) => g.key)).toEqual([
      "withholding",
      "fica",
      "liability",
      "payments",
      "result",
    ]);
    const liability = groups.find((g) => g.key === "liability")!;
    expect(liability.rows.filter((r) => r.estimate).map((r) => r.key)).toEqual([
      "federal_tax_ordinary_est",
      "federal_tax_preferential_est",
    ]);
    const niit = liability.rows.find(
      (r) => r.key === "net_investment_income_tax",
    );
    expect(niit?.amountMinor).toBeNull();
    expect(liability.rows.some((r) => r.key === "self_employment_tax")).toBe(
      false,
    );
  });
  it("reports unknown result and payments when there is no return", () => {
    const groups = taxBreakdownRows(
      summary({
        liability: [],
        payments_minor: null,
        refund_minor: null,
        amount_owed_minor: null,
      }),
    );
    expect(
      groups.find((g) => g.key === "payments")!.rows[0]!.amountMinor,
    ).toBeNull();
    expect(groups.find((g) => g.key === "result")!.rows[0]!.missingText).toBe(
      "unknown",
    );
  });
});

describe("yearOverYear / groupPayers / resolveYear", () => {
  it("orders years ascending", () => {
    const pts = yearOverYear([
      summary({ tax_year: 2024 }),
      summary({ tax_year: 2023, total_income_minor: 5 }),
    ]);
    expect(pts.map((p) => p.year)).toEqual([2023, 2024]);
    expect(pts[0]!.income).toBe(5);
  });
  it("keeps each year's own tax basis instead of mixing them silently", () => {
    const pts = yearOverYear([
      summary({ tax_year: 2024 }),
      summary({
        tax_year: 2023,
        headline: {
          federal_minor: 100,
          federal_basis: "estimate",
          fica_minor: 0,
          state_minor: 0,
          total_minor: 100,
        },
      }),
      summary({ tax_year: 2022, headline: null }),
    ]);
    expect(pts.map((p) => [p.year, p.basis, p.tax])).toEqual([
      [2022, null, 0],
      [2023, "estimate", 100],
      [2024, "return", 1489100 + 863244 + 500000],
    ]);
    expect(pts.map(yearPointLabel)).toEqual(["2022", "2023 (est.)", "2024"]);
  });
  it("groups payers for a year, largest first", () => {
    const rows = groupPayers(
      [...PAYERS, { ...PAYERS[0]!, tax_year: 2023 }],
      2024,
    );
    expect(rows.map((r) => r.payerName)).toEqual([
      "Pinecrest Logistics LLC",
      "Harbor Bank",
    ]);
    expect(
      groupPayers([{ ...PAYERS[0]!, payer_name: null }], 2024)[0]!.payerName,
    ).toBe("Unknown payer");
  });
  it("resolves the year", () => {
    expect(resolveYear(2023, [2024, 2023])).toBe(2023);
    expect(resolveYear(2020, [2024, 2023])).toBe(2024);
    expect(resolveYear(null, [])).toBeNull();
  });
});

describe("net-negative income categories", () => {
  const loss = summary({
    income_by_category: [
      {
        category: "salary_wages",
        label: "Wages and salary",
        amount_minor: 900000,
        is_memo: false,
      },
      {
        category: "interest",
        label: "Interest",
        amount_minor: 100000,
        is_memo: false,
      },
      {
        category: "long_term_capital_gain",
        label: "Long-term capital gains",
        amount_minor: -300000,
        is_memo: false,
      },
    ],
    total_income_minor: 700000,
  });
  it("keeps negative rows so rows sum to the total, but excludes them from the donut", () => {
    const bars = toIncomeBars(loss);
    expect(bars.map((b) => b.value)).toEqual([900000, 100000, -300000]);
    expect(bars.reduce((s, b) => s + b.value, 0)).toBe(loss.total_income_minor);
    expect(donutSlices(bars).map((b) => b.category)).toEqual([
      "salary_wages",
      "interest",
    ]);
  });
  it("computes share against positive categories and null for negatives", () => {
    const bars = toIncomeBars(loss);
    expect(incomeShare(bars, bars[0]!)).toBeCloseTo(0.9);
    expect(incomeShare(bars, bars[2]!)).toBeNull();
  });
});
