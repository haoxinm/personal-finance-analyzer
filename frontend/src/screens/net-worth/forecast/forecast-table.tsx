import { Money } from "@/components/primitives/money";

import type { DollarMode, ForecastRow } from "./forecast-logic";
import { formatUsd } from "./params";

export interface ForecastTableProps {
  /** Rows in the dollars of `mode`. */
  rows: readonly ForecastRow[];
  mode: DollarMode;
}

const HEAD = "py-2 pr-3 text-right font-medium";

/** The projection year by year: income, tax, spending, savings, investment return, one-off
 * events and the resulting net worth. */
export function ForecastTable({ rows, mode }: ForecastTableProps) {
  const usd = (n: number) => formatUsd(Math.round(n));
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm" data-testid="forecast-table">
        <caption className="sr-only">
          Year-by-year projection in {mode === "real" ? "today's" : "nominal"} dollars
        </caption>
        <thead>
          <tr className="border-b border-border text-xs text-muted-foreground">
            <th scope="col" className="py-2 pr-3 text-left font-medium">Year</th>
            <th scope="col" className={HEAD}>Income</th>
            <th scope="col" className={HEAD}>Tax</th>
            <th scope="col" className={HEAD}>Spending</th>
            <th scope="col" className={HEAD}>Savings</th>
            <th scope="col" className={HEAD}>Investment return</th>
            <th scope="col" className={HEAD}>One-off events</th>
            <th scope="col" className="py-2 text-right font-medium">Net worth</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.year} className="border-b border-border last:border-b-0">
              <th scope="row" className="py-2 pr-3 text-left font-medium">
                {r.year}
              </th>
              <td className="py-2 pr-3 text-right tabular-nums">{usd(r.income)}</td>
              <td className="py-2 pr-3 text-right tabular-nums">{usd(r.tax)}</td>
              <td className="py-2 pr-3 text-right tabular-nums">{usd(r.spending)}</td>
              <td className={`py-2 pr-3 text-right tabular-nums ${r.savings < 0 ? "text-outflow" : ""}`}>
                {r.savings < 0 ? <span aria-hidden="true">▼ </span> : null}
                {usd(r.savings)}
              </td>
              <td className="py-2 pr-3 text-right tabular-nums">{usd(r.investmentReturn)}</td>
              <td className="py-2 pr-3 text-right tabular-nums">{r.events === 0 ? "—" : usd(r.events)}</td>
              <td className="py-2 text-right font-medium">
                <Money tone="neutral" minorUnits={Math.round(r.netWorth)} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
