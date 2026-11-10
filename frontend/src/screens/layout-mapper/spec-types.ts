/**
 * Client-side mirror of `layout_spec.schema.json` and the semantic checks in
 * `personal_finance_analyzer.ingest.layout_spec._build_doc` (P1-H, A21, §2d.2, §2f.1).
 *
 * This is a *mirror*, not a replacement: the backend's `load_spec` remains the single source of
 * truth that runs when a spec is actually interpreted. It exists because — see the P3-G report —
 * `POST /api/layout-specs` currently persists `spec_yaml` verbatim with no server-side validation
 * at all, so without this module a malformed spec would only surface much later, as an opaque
 * `error_detail` on a failed extract/reparse job.
 */

export const KIND_HINTS = [
  "purchase",
  "refund",
  "payment",
  "transfer",
  "fee",
  "interest",
  "adjustment",
  "payment_or_refund",
] as const;
export type KindHint = (typeof KIND_HINTS)[number];

export const COLUMN_TYPES = ["date", "text", "money", "balance"] as const;
export type ColumnType = (typeof COLUMN_TYPES)[number];

export const MONEY_ROLES = ["debit", "credit", "amount"] as const;
export type MoneyRole = (typeof MONEY_ROLES)[number];

export const SECTIONS_MODES = ["heading", "column_header_row"] as const;
export type SectionsMode = (typeof SECTIONS_MODES)[number];

export const SIGN_CONVENTIONS = ["unsigned", "leading_minus"] as const;
export type SignConvention = (typeof SIGN_CONVENTIONS)[number];

export const YEAR_INFERENCES = ["from_period", "explicit"] as const;
export type YearInference = (typeof YEAR_INFERENCES)[number];

export const ACCOUNT_TYPES = ["credit", "checking", "savings"] as const;
export type AccountType = (typeof ACCOUNT_TYPES)[number];

export interface DetectSpec {
  all_of: string[];
  any_of: string[];
  score: number;
}

export interface ColumnSpec {
  name: string;
  x0: number;
  x1: number;
  type: ColumnType;
  formats?: string[];
  optional?: boolean;
  multiline?: boolean;
  role?: MoneyRole;
}

export interface SectionPatternSpec {
  match: string;
  kind_hint?: KindHint;
  outflow_kind_hint?: KindHint;
  inflow_kind_hint?: KindHint;
}

export interface SectionsSpec {
  mode?: SectionsMode;
  patterns?: SectionPatternSpec[];
  terminator?: string;
  exclude_tables?: string[];
}

export interface SignSpec {
  outflow?: SignConvention;
  inflow?: SignConvention;
}

export interface TotalsSpec {
  section_totals?: boolean;
}

export interface AccountMaskSpec {
  pattern: string;
}

export interface FxSpec {
  pattern: string;
  amount_group: string | number;
  currency_group: string | number;
  rate_group: string | number;
}

export interface LayoutSpecObject {
  id: string;
  version: number;
  account_type: AccountType;
  currency?: string;
  detect: DetectSpec;
  columns: ColumnSpec[];
  sections?: SectionsSpec;
  sign?: SignSpec;
  year_inference?: YearInference;
  totals?: TotalsSpec;
  account_mask?: AccountMaskSpec;
  fx?: FxSpec;
}

/** One field-level problem, mirroring `SpecFieldError` (`field`, `message`) in `layout_spec.py`. */
export interface SpecFieldError {
  field: string;
  message: string;
}

/** A fresh, empty column row for the manual mapper's column editor. */
export function emptyColumn(): ColumnSpec {
  return { name: "", x0: 0, x1: 0, type: "text" };
}

/** A fresh, empty spec for the manual mapper to start from. */
export function emptySpec(): LayoutSpecObject {
  return {
    id: "",
    version: 1,
    account_type: "credit",
    currency: "USD",
    detect: { all_of: [], any_of: [], score: 0.85 },
    columns: [
      { name: "posted_date", x0: 0, x1: 0, type: "date", formats: ["%m/%d"] },
      { name: "description", x0: 0, x1: 0, type: "text", multiline: true },
      { name: "amount", x0: 0, x1: 0, type: "money" },
    ],
    sections: { mode: "heading", patterns: [], exclude_tables: [] },
    sign: { outflow: "unsigned", inflow: "leading_minus" },
    year_inference: "from_period",
    totals: { section_totals: false },
  };
}
