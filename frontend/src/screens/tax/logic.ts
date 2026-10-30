import { ApiError, type components } from "@/api/client";
import { parseBalanceInput } from "@/screens/net-worth/logic";

export type TaxSummary = components["schemas"]["TaxYearSummary"];
export type TaxDocument = components["schemas"]["TaxDocument"];
export type TaxIncomePayer = components["schemas"]["TaxIncomePayer"];
export type TaxFormType = components["schemas"]["TaxFormType"];
export type TaxCategoryAmount = components["schemas"]["TaxCategoryAmount"];
export type TaxDocumentDetail = components["schemas"]["TaxDocumentDetail"];
export type TaxFormDetail = components["schemas"]["TaxFormDetail"];
export type IncomeItem = components["schemas"]["IncomeItemOut"];
export type TaxItem = components["schemas"]["TaxItemOut"];
export type IncomeCategory = components["schemas"]["IncomeCategoryEnum"];
export type TaxCategory = components["schemas"]["TaxCategoryEnum"];
export type SkippedFrom = components["schemas"]["TaxSkippedFrom"];
export type FilingStatus = NonNullable<
  components["schemas"]["TaxFormCreate"]["filing_status"]
>;

/** "0.1319" -> "13.19%"; null/undefined/NaN -> an em dash (rate unknown). */
export function formatRate(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || Number.isNaN(rate)) return "—";
  return `${(rate * 100).toFixed(2)}%`;
}

export function incomeBasisLabel(basis: TaxSummary["income_basis"]): string {
  switch (basis) {
    case "source_documents":
      return "W-2 / 1099 source forms";
    case "return":
      return "Form 1040 return";
    case "mixed":
      return "Mixed: source forms and return";
    case "none":
      return "No income documents";
  }
}

/** Banner text for an unusual basis, or null for the normal cases: income from the Form 1040 when
 * one is imported, otherwise from W-2 / 1099 source forms. Mismatches between the two arrive as
 * separate `warnings`. */
export function basisNotice(basis: TaxSummary["income_basis"]): string | null {
  switch (basis) {
    case "mixed":
      return "Some users' income comes from their Form 1040 and others' from W-2 / 1099 source forms only.";
    case "none":
      return "No income was found in the imported documents for this year.";
    default:
      return null;
  }
}

const FORM_LABELS: Record<TaxFormType, string> = {
  w2: "W-2",
  "1099-int": "1099-INT",
  "1099-div": "1099-DIV",
  "1099-b": "1099-B",
  "1099-nec": "1099-NEC",
  "1099-misc": "1099-MISC",
  "1099-consolidated": "1099 (consolidated)",
  "1040": "1040",
  "state-return": "State return",
};

export function formTypeLabel(form: string): string {
  return FORM_LABELS[form as TaxFormType] ?? form;
}

export function documentStatusLabel(status: string): string {
  switch (status) {
    case "parsed":
      return "Parsed";
    case "unsupported":
      return "Unsupported";
    case "error":
      return "Error";
    case "duplicate":
      return "Already imported";
    default:
      return status;
  }
}

function amountOf(
  rows: readonly TaxCategoryAmount[],
  category: string,
): number | null {
  const row = rows.find((r) => r.category === category);
  return row ? row.amount_minor : null;
}

export interface HeadlineTax {
  /** Federal income tax: the return's total liability, else what was withheld. */
  federalMinor: number;
  federalKind: "liability" | "withheld";
  ficaMinor: number;
  /** State (and local) income tax: liability when printed, else withheld. */
  stateMinor: number;
  totalMinor: number;
}

export function headlineTax(s: TaxSummary): HeadlineTax {
  const liability = amountOf(s.liability, "total_federal_tax");
  const federalKind = liability !== null ? "liability" : "withheld";
  const federalMinor =
    liability ?? amountOf(s.withheld, "federal_income_tax_withheld") ?? 0;
  const ficaMinor = s.fica.reduce((sum, r) => sum + r.amount_minor, 0);
  const stateLiability = amountOf(s.liability, "state_income_tax_liability");
  const stateMinor =
    stateLiability ??
    (amountOf(s.withheld, "state_income_tax_withheld") ?? 0) +
      (amountOf(s.withheld, "local_income_tax_withheld") ?? 0);
  return {
    federalMinor,
    federalKind,
    ficaMinor,
    stateMinor,
    totalMinor: federalMinor + ficaMinor + stateMinor,
  };
}

export interface RefundOrOwed {
  kind: "refund" | "owed" | "even" | "unknown";
  amountMinor: number | null;
  derived: boolean;
}

export function refundOrOwed(s: TaxSummary): RefundOrOwed {
  const refund = s.refund_minor;
  const owed = s.amount_owed_minor;
  const derived = s.refund_or_owed_derived;
  if (refund === null && owed === null)
    return { kind: "unknown", amountMinor: null, derived };
  if ((refund ?? 0) > 0)
    return { kind: "refund", amountMinor: refund, derived };
  if ((owed ?? 0) > 0) return { kind: "owed", amountMinor: owed, derived };
  return { kind: "even", amountMinor: 0, derived };
}

export interface IncomeBar {
  category: string;
  label: string;
  value: number;
}

/** Non-memo, non-zero income categories for the table (memo lines are never part of the total).
 * Net-negative categories (e.g. capital losses) are kept so the rows sum to the total. */
export function toIncomeBars(s: TaxSummary): IncomeBar[] {
  return s.income_by_category
    .filter((r) => !r.is_memo && r.amount_minor !== 0)
    .map((r) => ({
      category: r.category,
      label: r.label,
      value: r.amount_minor,
    }));
}

/** Only positive categories can be donut slices. */
export function donutSlices(bars: readonly IncomeBar[]): IncomeBar[] {
  return bars.filter((b) => b.value > 0);
}

/** Share of a positive category against the sum of positive categories; null for non-positive rows. */
export function incomeShare(
  bars: readonly IncomeBar[],
  bar: IncomeBar,
): number | null {
  if (bar.value <= 0) return null;
  const positive = bars.reduce(
    (sum, b) => (b.value > 0 ? sum + b.value : sum),
    0,
  );
  return positive > 0 ? bar.value / positive : null;
}

export interface BreakdownRow {
  key: string;
  label: string;
  /** null = not reported / unknown. */
  amountMinor: number | null;
  estimate?: boolean;
  /** Shown instead of an amount when `amountMinor` is null. */
  missingText?: string;
}

export interface BreakdownGroup {
  key: string;
  title: string;
  rows: BreakdownRow[];
}

const LIABILITY_ROWS: Array<{
  category: string;
  label: string;
  estimate?: boolean;
}> = [
  { category: "total_federal_tax", label: "Total federal tax" },
  {
    category: "federal_tax_ordinary_est",
    label: "Ordinary income tax",
    estimate: true,
  },
  {
    category: "federal_tax_preferential_est",
    label: "Preferential-rate tax (dividends and gains)",
    estimate: true,
  },
  {
    category: "net_investment_income_tax",
    label: "Net investment income tax (NIIT)",
  },
  { category: "additional_medicare_tax", label: "Additional Medicare tax" },
  { category: "self_employment_tax", label: "Self-employment tax" },
  { category: "state_income_tax_liability", label: "State income tax" },
];

/** Grouped rows for the tax table. Liability rows show only what the return printed, except NIIT,
 * which reads "not reported" when absent (the return may simply have none). */
export function taxBreakdownRows(s: TaxSummary): BreakdownGroup[] {
  const groups: BreakdownGroup[] = [
    {
      key: "withholding",
      title: "Withholding",
      rows: s.withheld.map((r) => ({
        key: r.category,
        label: r.label,
        amountMinor: r.amount_minor,
      })),
    },
    {
      key: "fica",
      title: "FICA",
      rows: s.fica.map((r) => ({
        key: r.category,
        label: r.label,
        amountMinor: r.amount_minor,
      })),
    },
  ];
  const liabilityRows: BreakdownRow[] = [];
  for (const spec of LIABILITY_ROWS) {
    const amount = amountOf(s.liability, spec.category);
    if (amount === null && spec.category !== "net_investment_income_tax")
      continue;
    liabilityRows.push({
      key: spec.category,
      label: spec.label,
      amountMinor: amount,
      estimate: spec.estimate,
      missingText: "not reported",
    });
  }
  groups.push({
    key: "liability",
    title: "Liability (from return)",
    rows: liabilityRows,
  });
  groups.push({
    key: "payments",
    title: "Payments",
    rows: [
      {
        key: "payments",
        label: "Total payments",
        amountMinor: s.payments_minor,
        missingText: "not reported",
      },
    ],
  });
  const result = refundOrOwed(s);
  const resultRows: BreakdownRow[] = [];
  if (
    result.kind === "refund" ||
    result.kind === "owed" ||
    result.kind === "even"
  ) {
    resultRows.push({
      key: "result",
      label: result.kind === "owed" ? "Amount owed" : "Refund",
      amountMinor: result.amountMinor,
    });
  } else {
    resultRows.push({
      key: "result",
      label: "Refund / amount owed",
      amountMinor: null,
      missingText: "unknown",
    });
  }
  groups.push({ key: "result", title: "Result", rows: resultRows });
  return groups;
}

export interface YearPoint {
  year: number;
  income: number;
  tax: number;
}

/** Oldest-to-newest points for the year-over-year chart. */
export function yearOverYear(summaries: readonly TaxSummary[]): YearPoint[] {
  return [...summaries]
    .sort((a, b) => a.tax_year - b.tax_year)
    .map((s) => ({
      year: s.tax_year,
      income: s.total_income_minor,
      tax: headlineTax(s).totalMinor,
    }));
}

export interface PayerRow {
  key: string;
  payerName: string;
  formType: string;
  totalMinor: number;
  lines: Array<{ category: string; label: string; amountMinor: number }>;
}

/** One row per (payer, form type) for the selected year, largest first; users are combined. */
export function groupPayers(
  rows: readonly TaxIncomePayer[],
  year: number,
): PayerRow[] {
  const map = new Map<string, PayerRow>();
  for (const r of rows) {
    if (r.tax_year !== year) continue;
    const payerName = r.payer_name?.trim() || "Unknown payer";
    const key = `${payerName}|${r.form_type}`;
    let row = map.get(key);
    if (!row) {
      row = { key, payerName, formType: r.form_type, totalMinor: 0, lines: [] };
      map.set(key, row);
    }
    row.totalMinor += r.amount_minor;
    const line = row.lines.find((l) => l.category === r.category);
    if (line) line.amountMinor += r.amount_minor;
    else
      row.lines.push({
        category: r.category,
        label: r.label,
        amountMinor: r.amount_minor,
      });
  }
  return [...map.values()].sort((a, b) => b.totalMinor - a.totalMinor);
}

/** Selected year if still available, else the newest. */
export function resolveYear(
  selected: number | null,
  years: readonly number[],
): number | null {
  if (selected !== null && years.includes(selected)) return selected;
  return years[0] ?? null;
}

export const TAX_PRIVACY_NOTE =
  "Only the last 4 digits of ID numbers are kept; files stay on this computer.";

/** What is masked before page text goes to the LLM (shown next to the preview). */
export const LLM_READ_MASKED =
  "Names, addresses, taxpayer ID numbers (SSN/EIN), emails, phone and account numbers";

// ---- Manual overrides ------------------------------------------------------------------------

export interface Option<T extends string = string> {
  value: T;
  label: string;
}

export const INCOME_CATEGORY_OPTIONS: Option<IncomeCategory>[] = [
  { value: "salary_wages", label: "Wages and salary" },
  { value: "interest", label: "Interest" },
  { value: "ordinary_dividends", label: "Ordinary dividends" },
  { value: "qualified_dividends", label: "Qualified dividends (memo)" },
  { value: "short_term_capital_gain", label: "Short-term capital gain" },
  { value: "long_term_capital_gain", label: "Long-term capital gain" },
  { value: "self_employment", label: "Self-employment" },
  { value: "other", label: "Other income" },
];

export const TAX_CATEGORY_OPTIONS: Option<TaxCategory>[] = [
  {
    value: "federal_income_tax_withheld",
    label: "Federal income tax withheld",
  },
  { value: "state_income_tax_withheld", label: "State income tax withheld" },
  { value: "local_income_tax_withheld", label: "Local income tax withheld" },
  { value: "social_security_tax", label: "Social Security tax" },
  { value: "medicare_tax", label: "Medicare tax" },
  { value: "total_federal_tax", label: "Total federal tax" },
  {
    value: "federal_tax_ordinary_est",
    label: "Ordinary income tax (estimate)",
  },
  {
    value: "federal_tax_preferential_est",
    label: "Preferential-rate tax (estimate)",
  },
  { value: "net_investment_income_tax", label: "Net investment income tax" },
  { value: "additional_medicare_tax", label: "Additional Medicare tax" },
  { value: "self_employment_tax", label: "Self-employment tax" },
  { value: "state_income_tax_liability", label: "State income tax" },
  { value: "estimated_tax_payments", label: "Estimated tax payments" },
  { value: "total_payments", label: "Total payments" },
  { value: "refund", label: "Refund" },
  { value: "amount_owed", label: "Amount owed" },
];

export const FORM_TYPE_OPTIONS: Option<TaxFormType>[] = (
  Object.keys(FORM_LABELS) as TaxFormType[]
).map((value) => ({ value, label: FORM_LABELS[value] }));

export const FILING_STATUS_OPTIONS: Option<FilingStatus>[] = [
  { value: "single", label: "Single" },
  { value: "mfj", label: "Married filing jointly" },
  { value: "mfs", label: "Married filing separately" },
  { value: "hoh", label: "Head of household" },
  { value: "qss", label: "Qualifying surviving spouse" },
];

export function categoryLabel(
  kind: "income" | "tax",
  category: string,
): string {
  const options: Option[] =
    kind === "income" ? INCOME_CATEGORY_OPTIONS : TAX_CATEGORY_OPTIONS;
  return options.find((o) => o.value === category)?.label ?? category;
}

/** Only capital gains may be negative (losses). */
export function isSignedIncomeCategory(category: string): boolean {
  return (
    category === "short_term_capital_gain" ||
    category === "long_term_capital_gain"
  );
}

/** Dollars typed by the user -> integer cents, or null when it is not a clear amount. Accepts
 * "$1,234.56", "12", "7.5" and (when `allowNegative`) "-50" or "(50.00)"; rounds half-up. */
export function dollarsToMinor(
  text: string,
  allowNegative = false,
): number | null {
  let t = text.trim();
  let negative = false;
  if (/^\(.*\)$/.test(t)) {
    negative = true;
    t = t.slice(1, -1).trim();
  } else if (t.startsWith("-")) {
    negative = true;
    t = t.slice(1).trim();
  }
  const minor = parseBalanceInput(t);
  if (minor === null) return null;
  if (negative && !allowNegative) return null;
  return negative ? -minor : minor;
}

/** Integer cents -> "1234.56" (no grouping, so it round-trips through `dollarsToMinor`). */
export function minorToDollars(minor: number): string {
  const abs = Math.abs(minor);
  const text = `${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
  return minor < 0 ? `-${text}` : text;
}

/** Validation message for a typed amount, or null when it is fine. */
export function amountError(text: string, signed: boolean): string | null {
  if (text.trim() === "") return "Enter an amount";
  const minor = dollarsToMinor(text, signed);
  if (minor === null) {
    return /^\s*[-(]/.test(text) && !signed
      ? "Amount cannot be negative"
      : "Enter a valid dollar amount";
  }
  return null;
}

/** Last-4 digits: empty is fine (null), otherwise exactly 4 digits. */
export function last4Error(text: string): string | null {
  const t = text.trim();
  return t === "" || /^\d{4}$/.test(t) ? null : "Enter exactly 4 digits";
}

export function taxYearError(text: string): string | null {
  const n = Number(text);
  return text.trim() !== "" && Number.isInteger(n) && n >= 2000 && n <= 2099
    ? null
    : "Enter a year from 2000 to 2099";
}

export function blankToNull(text: string): string | null {
  const t = text.trim();
  return t === "" ? null : t;
}

export type FormLike = Pick<
  TaxFormDetail,
  "extraction_method" | "is_user_edited" | "needs_review"
>;

export function provenanceLabel(form: FormLike): string {
  if (form.needs_review) return "Read by LLM — needs review";
  if (form.extraction_method === "manual") return "Entered manually";
  if (form.is_user_edited) return "Edited";
  if (form.extraction_method === "llm") return "Read by LLM — confirmed";
  return "Parsed";
}

export function itemOriginLabel(
  origin: "parser" | "llm" | "manual",
): string | null {
  if (origin === "llm") return "Read by LLM";
  if (origin === "manual") return "Edited";
  return null;
}

/** One sentence per source file, e.g. `1 form was already imported from "x.pdf" and was skipped.` */
export function skippedFromMessages(skipped: readonly SkippedFrom[]): string[] {
  const counts = new Map<string, number>();
  for (const s of skipped)
    counts.set(s.original_name, (counts.get(s.original_name) ?? 0) + 1);
  return [...counts].map(([name, n]) =>
    n === 1
      ? `1 form was already imported from "${name}" and was skipped.`
      : `${n} forms were already imported from "${name}" and were skipped.`,
  );
}

/** Readable text for an API failure, including FastAPI's list-style 422 details. */
export function errorMessage(
  e: unknown,
  fallback = "Something went wrong.",
): string {
  if (e instanceof ApiError && Array.isArray(e.detail)) {
    const parts = (e.detail as unknown[])
      .map((d) =>
        typeof d === "object" &&
        d !== null &&
        "msg" in d &&
        typeof d.msg === "string"
          ? d.msg
          : null,
      )
      .filter((m): m is string => m !== null);
    if (parts.length > 0) return parts.join("; ");
  }
  return e instanceof Error && e.message ? e.message : fallback;
}
