import type { components } from "@/api/client";

export type AnalyticsRow = components["schemas"]["AnalyticsRow"];
export type TopMerchantRow = components["schemas"]["TopMerchantRow"];
export type Account = components["schemas"]["Account"];
export type User = components["schemas"]["User"];

export interface CategoryPeriodPoint {
  period: string;
  /** category key -> signed minor units for that period */
  categories: Record<string, number>;
  totalMinor: number;
}

/**
 * Pivots `AnalyticsRow[]` grouped by `["period", "category"]` into one point per period, with
 * every category's total keyed by its category key — the shape a stacked bar chart wants
 * (§3.9 returns tidy rows; the chart needs them wide). A row with no `category` (never
 * classified, or the query did not group by it) falls into `"uncategorized"` rather than being
 * dropped, so totals still reconcile.
 */
export function pivotByCategory(rows: readonly AnalyticsRow[]): CategoryPeriodPoint[] {
  const byPeriod = new Map<string, CategoryPeriodPoint>();
  for (const row of rows) {
    const period = row.period ?? "unknown";
    const category = row.category ?? "uncategorized";
    let point = byPeriod.get(period);
    if (!point) {
      point = { period, categories: {}, totalMinor: 0 };
      byPeriod.set(period, point);
    }
    point.categories[category] = (point.categories[category] ?? 0) + row.total_minor;
    point.totalMinor += row.total_minor;
  }
  return Array.from(byPeriod.values()).sort((a, b) => a.period.localeCompare(b.period));
}

/** Every distinct category key present across a pivoted series, sorted for a stable stack order
 * and legend (so a category's colour and stack position never jump between renders). */
export function distinctCategories(points: readonly CategoryPeriodPoint[]): string[] {
  const set = new Set<string>();
  for (const point of points) {
    for (const key of Object.keys(point.categories)) set.add(key);
  }
  return Array.from(set).sort();
}

export interface TrendPoint {
  period: string;
  totalMinor: number;
}

/** Collapses `AnalyticsRow[]` grouped by `["period"]` into one signed total per period, sorted
 * chronologically (period keys are zero-padded strings, e.g. `"2025-03"`, so lexical order is
 * chronological order). */
export function toTrendPoints(rows: readonly AnalyticsRow[]): TrendPoint[] {
  const byPeriod = new Map<string, number>();
  for (const row of rows) {
    const period = row.period ?? "unknown";
    byPeriod.set(period, (byPeriod.get(period) ?? 0) + row.total_minor);
  }
  return Array.from(byPeriod.entries())
    .map(([period, totalMinor]) => ({ period, totalMinor }))
    .sort((a, b) => a.period.localeCompare(b.period));
}

export interface MonthOverMonthDelta {
  currentPeriod: string;
  previousPeriod: string;
  currentMinor: number;
  previousMinor: number;
  deltaMinor: number;
  /** `null` when the previous period totalled exactly zero — a percentage change is undefined,
   * not infinite or zero, so callers must render a dash rather than a misleading number. */
  deltaPct: number | null;
}

/** The most recent period-over-period change in the trend series. Needs at least two periods;
 * returns `null` otherwise (a single-period or empty series has nothing to compare). */
export function computeMonthOverMonthDelta(
  points: readonly TrendPoint[],
): MonthOverMonthDelta | null {
  if (points.length < 2) return null;
  const sorted = [...points].sort((a, b) => a.period.localeCompare(b.period));
  const current = sorted[sorted.length - 1]!;
  const previous = sorted[sorted.length - 2]!;
  const deltaMinor = current.totalMinor - previous.totalMinor;
  const deltaPct = previous.totalMinor === 0 ? null : deltaMinor / Math.abs(previous.totalMinor);
  return {
    currentPeriod: current.period,
    previousPeriod: previous.period,
    currentMinor: current.totalMinor,
    previousMinor: previous.totalMinor,
    deltaMinor,
    deltaPct,
  };
}

export interface UserTotal {
  userId: number;
  totalMinor: number;
}

/** Collapses `AnalyticsRow[]` grouped by `["user"]` into one signed total per `user_id`, ranked
 * highest spend first. Rows without a `user` (the query did not group by it) are dropped rather
 * than lumped into a fake bucket. */
export function totalsByUser(rows: readonly AnalyticsRow[]): UserTotal[] {
  const byUser = new Map<number, number>();
  for (const row of rows) {
    if (row.user === null || row.user === undefined) continue;
    byUser.set(row.user, (byUser.get(row.user) ?? 0) + row.total_minor);
  }
  return Array.from(byUser.entries())
    .map(([userId, totalMinor]) => ({ userId, totalMinor }))
    .sort((a, b) => b.totalMinor - a.totalMinor);
}

/**
 * `TopMerchantRow.merchant` is the raw `merchant_key` (§3.3): lowercase, and the analytics
 * rollup has no join back to a human display name (no `merchant_canonical`, no representative
 * `description_clean` — see this function's call site for why that would be the better fix).
 * Until the API surfaces one, title-case the key as a legible display label; the caller keeps
 * the untouched key as a `title` tooltip so the full value is never actually lost, just no
 * longer the *only* thing shown.
 */
export function humanizeMerchantKey(merchantKey: string): string {
  return merchantKey
    .split(/\s+/)
    .filter(Boolean)
    .map((word) => (word[0]?.toUpperCase() ?? "") + word.slice(1))
    .join(" ");
}

/**
 * D5: aggregations always filter to a single posted currency; this tells the UI whether any
 * *other* currency exists among the accounts in scope, so it can banner "these totals exclude
 * N accounts in EUR" rather than silently under-counting. Never converts — only detects.
 */
export function detectOtherCurrencies(
  accounts: readonly Pick<Account, "currency">[],
  selectedCurrency: string,
): string[] {
  const others = new Set<string>();
  for (const account of accounts) {
    if (account.currency && account.currency !== selectedCurrency) others.add(account.currency);
  }
  return Array.from(others).sort();
}

/** Key of the merged "everything else" series/slice in the pie and line charts. */
export const OTHER_KEY = "__other__";

export interface CategoryTotal {
  key: string;
  totalMinor: number;
}

/** Each category's signed total summed across every period, largest first. A row with no
 * category falls into `"uncategorized"`, as in `pivotByCategory`. */
export function totalsByCategory(rows: readonly AnalyticsRow[]): CategoryTotal[] {
  const totals = new Map<string, number>();
  for (const row of rows) {
    const key = row.category ?? "uncategorized";
    totals.set(key, (totals.get(key) ?? 0) + row.total_minor);
  }
  return Array.from(totals.entries())
    .map(([key, totalMinor]) => ({ key, totalMinor }))
    .sort((a, b) => b.totalMinor - a.totalMinor || a.key.localeCompare(b.key));
}

export interface PieSlice {
  key: string;
  totalMinor: number;
  /** Share of the positive total, 0-100. */
  percent: number;
  /** For the merged `OTHER_KEY` slice: the category keys folded into it. */
  members?: string[];
}

export interface PieSlices {
  slices: PieSlice[];
  /** Categories whose total is <= 0 (refunds exceed spending): not drawable as a share. */
  excluded: string[];
  /** Sum of the positive totals, i.e. what the slices add up to. */
  totalMinor: number;
}

/**
 * Turns category totals into pie slices: only positive totals, largest first; above `maxSlices`
 * the largest `maxSlices` stay and the rest merge into one `OTHER_KEY` slice. Categories with
 * total <= 0 are returned in `excluded` instead of being silently dropped.
 */
export function pieSlicesFromTotals(totals: readonly CategoryTotal[], maxSlices = 8): PieSlices {
  const positive = totals.filter((t) => t.totalMinor > 0).sort((a, b) => b.totalMinor - a.totalMinor);
  const excluded = totals.filter((t) => t.totalMinor <= 0).map((t) => t.key);
  const totalMinor = positive.reduce((sum, t) => sum + t.totalMinor, 0);
  let kept: CategoryTotal[] = positive;
  let members: string[] | undefined;
  if (positive.length > maxSlices) {
    const tail = positive.slice(maxSlices);
    members = tail.map((t) => t.key);
    const rest = tail.reduce((sum, t) => sum + t.totalMinor, 0);
    kept = [...positive.slice(0, maxSlices), { key: OTHER_KEY, totalMinor: rest }];
  }
  const slices: PieSlice[] = kept.map((t) => ({
    key: t.key,
    totalMinor: t.totalMinor,
    percent: totalMinor === 0 ? 0 : (t.totalMinor / totalMinor) * 100,
    ...(t.key === OTHER_KEY && members ? { members } : {}),
  }));
  return { slices, excluded, totalMinor };
}

export type CategoryLineRow = { period: string } & Record<string, number | string>;

/**
 * Wide data for a one-line-per-category chart: the `n` categories with the largest absolute
 * total across the range, plus `OTHER_KEY` summing the rest when there are more. Missing
 * periods count as 0. `keys` lists the series in legend order (top categories, then Other).
 */
export function topCategoriesWithOther(
  points: readonly CategoryPeriodPoint[],
  categories: readonly string[],
  n = 8,
): { keys: string[]; data: CategoryLineRow[] } {
  const absTotal = (key: string) =>
    Math.abs(points.reduce((sum, p) => sum + (p.categories[key] ?? 0), 0));
  const ranked = [...categories].sort((a, b) => absTotal(b) - absTotal(a) || a.localeCompare(b));
  const top = ranked.slice(0, n);
  const rest = ranked.slice(n);
  const keys = rest.length > 0 ? [...top, OTHER_KEY] : top;
  const data = points.map((point) => {
    const row: CategoryLineRow = { period: point.period };
    for (const key of top) row[key] = point.categories[key] ?? 0;
    if (rest.length > 0) row[OTHER_KEY] = rest.reduce((sum, key) => sum + (point.categories[key] ?? 0), 0);
    return row;
  });
  return { keys, data };
}

/** Display name for a category key when no taxonomy name is known. */
export function defaultCategoryLabel(key: string): string {
  // Not "Other(s)": the taxonomy has a real "Others" category this must not be confused with.
  if (key === OTHER_KEY) return "All other categories";
  if (key === "uncategorized") return "Uncategorized";
  return key;
}

/** The Transactions-grid query for a pie slice / legend click. The "uncategorized" pseudo-key
 * becomes the grid's `uncategorized=true` filter (`category_keys=uncategorized` matches nothing);
 * the Overview's currency, users and dates are carried so the list matches the slice. */
export function buildCategoryDrillParams(
  key: string,
  filters: { currency: string; userIds?: readonly number[]; dateFrom?: string; dateTo?: string },
): URLSearchParams {
  const next = new URLSearchParams();
  if (key === "uncategorized") next.set("uncategorized", "true");
  else next.set("category_keys", key);
  next.set("currency", filters.currency);
  for (const id of filters.userIds ?? []) next.append("user_ids", String(id));
  if (filters.dateFrom) next.set("date_from", filters.dateFrom);
  if (filters.dateTo) next.set("date_to", filters.dateTo);
  return next;
}
