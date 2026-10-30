import type { ForecastRow } from "./forecast-logic";
import type { HistoryRow } from "./params";

export interface ChartPoint {
  year: number;
  history?: number;
  projection?: number;
}

/** Chart rows: actual history, and the projection starting from the base-year value. */
export function chartData(
  history: readonly HistoryRow[],
  rows: readonly ForecastRow[],
  baseYear: number,
  startNetWorthMinor: number,
): ChartPoint[] {
  const byYear = new Map<number, ChartPoint>();
  const point = (year: number) => {
    let p = byYear.get(year);
    if (!p) byYear.set(year, (p = { year }));
    return p;
  };
  for (const h of history) {
    if (h.net_worth_minor != null) point(h.year).history = h.net_worth_minor;
  }
  point(baseYear).projection = startNetWorthMinor;
  for (const r of rows) point(r.year).projection = r.netWorth;
  return [...byYear.values()].sort((a, b) => a.year - b.year);
}
