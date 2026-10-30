import type { components } from "@/api/client";

type Entry = components["schemas"]["BalanceEntry"];
type Snapshot = components["schemas"]["NetWorthSnapshot"];
type Point = components["schemas"]["NetWorthPoint"];
type AccountSeries = components["schemas"]["NetWorthAccountSeries"];
type User = components["schemas"]["User"];

export type Category = "checking" | "savings" | "investment" | "credit_card";

export const CATEGORIES: readonly Category[] = ["checking", "savings", "investment", "credit_card"];
/** The "debit" side: shown in the pie. */
export const ASSET_CATEGORIES: readonly Category[] = ["checking", "savings", "investment"];

export const CATEGORY_LABELS: Record<string, string> = {
  checking: "Checking",
  savings: "Savings",
  investment: "Investment",
  credit_card: "Credit card",
};

export function categoryLabel(category: string): string {
  return CATEGORY_LABELS[category] ?? category;
}

const MONTH_NAMES = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];
export const MONTH_LABELS = MONTH_NAMES;

/** "2025-03" -> "Mar 2025"; anything unparsable is returned unchanged. */
export function periodLabel(period: string): string {
  const match = /^(\d{4})-(\d{2})$/.exec(period);
  if (!match) return period;
  const month = Number(match[2]);
  const name = MONTH_NAMES[month - 1];
  return name ? `${name} ${match[1]}` : period;
}

export function toPeriod(year: number, month: number): string {
  return `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}`;
}

export function entryPeriod(entry: Pick<Entry, "year" | "month">): string {
  return toPeriod(entry.year, entry.month);
}

/** Distinct account names for a category, most recent entry first, case-insensitive dedupe
 * (the first spelling seen, i.e. the most recent, wins). */
export function accountSuggestions(entries: readonly Entry[], category: string): string[] {
  const sorted = entries
    .filter((e) => e.category === category)
    .sort((a, b) => b.year - a.year || b.month - a.month);
  const seen = new Set<string>();
  const out: string[] = [];
  for (const e of sorted) {
    const key = e.account_name.trim().toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(e.account_name);
  }
  return out;
}

export interface CategoryRow {
  period: string;
  checking: number;
  savings: number;
  investment: number;
  credit_card: number;
  net_worth: number;
}

export function categorySeriesRows(series: readonly Point[]): CategoryRow[] {
  return series.map((p) => ({
    period: p.period,
    checking: p.by_category["checking"] ?? 0,
    savings: p.by_category["savings"] ?? 0,
    investment: p.by_category["investment"] ?? 0,
    credit_card: p.by_category["credit_card"] ?? 0,
    net_worth: p.net_worth_minor,
  }));
}

export interface AccountLineKey {
  key: string;
  label: string;
  category: string;
}

export type AccountRow = { period: string } & Record<string, number | string | boolean | undefined>;

/** Wide rows for a multi-line chart: one column per account. `carried:<key>` flags carried
 * points so the chart can render them as hollow dots. In all-users mode (`users` given and
 * more than one account owner) labels gain the owner's name. */
export function accountSeriesRows(
  accountSeries: readonly AccountSeries[],
  users: readonly User[] = [],
): { rows: AccountRow[]; keys: AccountLineKey[] } {
  const owners = new Set(accountSeries.map((s) => s.user_id));
  const multiUser = owners.size > 1;
  const nameOf = (id: number) => users.find((u) => u.id === id)?.name ?? `User ${id}`;

  const keys: AccountLineKey[] = accountSeries.map((s) => ({
    key: `${s.user_id}:${s.category}:${s.account_name}`,
    label: multiUser ? `${s.account_name} (${nameOf(s.user_id)})` : s.account_name,
    category: s.category,
  }));

  const byPeriod = new Map<string, AccountRow>();
  accountSeries.forEach((s, i) => {
    const key = keys[i]!.key;
    for (const pt of s.points) {
      let row = byPeriod.get(pt.period);
      if (!row) {
        row = { period: pt.period };
        byPeriod.set(pt.period, row);
      }
      row[key] = pt.balance_minor;
      row[`carried:${key}`] = pt.carried;
    }
  });
  const rows = Array.from(byPeriod.values()).sort((a, b) => a.period.localeCompare(b.period));
  return { rows, keys };
}

export interface PieSlice {
  category: Category;
  label: string;
  value: number;
  percent: number;
}

/** Debit categories only (checking/savings/investment) with a positive balance. */
export function pieSlices(snapshot: Snapshot | null | undefined): PieSlice[] {
  if (!snapshot) return [];
  const raw = snapshot.categories.filter(
    (c) => (ASSET_CATEGORIES as readonly string[]).includes(c.category) && c.balance_minor > 0,
  );
  const total = raw.reduce((sum, c) => sum + c.balance_minor, 0);
  return raw.map((c) => ({
    category: c.category,
    label: categoryLabel(c.category),
    value: c.balance_minor,
    percent: total > 0 ? (c.balance_minor / total) * 100 : 0,
  }));
}

/** Parses a dollar amount ("1,234.56", "$12", "7.5") into minor units, rounding half-up at
 * two decimals ("1.005" -> 101). Returns null for empty, negative or non-numeric input. */
export function parseBalanceInput(raw: string): number | null {
  const cleaned = raw.trim().replace(/^\$/, "").replace(/,/g, "");
  if (!/^\d*\.?\d+$|^\d+\.$/.test(cleaned)) return null;
  const [whole = "0", frac = ""] = cleaned.split(".");
  const cents = Number(frac.padEnd(3, "0").slice(0, 3));
  const base = Number(whole || "0") * 100 + Math.floor(cents / 10);
  const minor = base + (cents % 10 >= 5 ? 1 : 0);
  return Number.isSafeInteger(minor) ? minor : null;
}

export function formatBalanceInput(minor: number): string {
  return (minor / 100).toFixed(2);
}

/** `-` for a missing change, else a signed delta label helper for tests/aria. */
export function changeDirection(change: number | null | undefined): "up" | "down" | "flat" | "none" {
  if (change === null || change === undefined) return "none";
  return change > 0 ? "up" : change < 0 ? "down" : "flat";
}
