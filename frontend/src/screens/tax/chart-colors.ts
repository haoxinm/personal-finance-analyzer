/** Fixed per-category income colours (`hsl(var(--chart-*))` tokens, themed in index.css). Distinct from the
 * spending outflow/inflow hues so income never reads as spending. */
export const INCOME_CATEGORY_COLORS: Record<string, string> = {
  salary_wages: "hsl(var(--chart-sky))",
  interest: "hsl(var(--chart-emerald))",
  ordinary_dividends: "hsl(var(--chart-violet))",
  qualified_dividends: "hsl(var(--chart-violet-soft))",
  short_term_capital_gain: "hsl(var(--chart-amber))",
  long_term_capital_gain: "hsl(var(--chart-lime))",
  self_employment: "hsl(var(--chart-cyan))",
  other: "hsl(var(--chart-slate-soft))",
};

export const CHART_INCOME = "hsl(var(--chart-sky))";
export const CHART_TAX = "hsl(var(--chart-amber))";

export function incomeColor(category: string): string {
  return INCOME_CATEGORY_COLORS[category] ?? "hsl(var(--chart-slate-soft))";
}
