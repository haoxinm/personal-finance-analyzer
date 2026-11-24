import * as React from "react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { Money } from "@/components/primitives/money";
import { EmptyState } from "@/components/primitives/states";
import { CHART_TOOLTIP_PROPS } from "@/lib/chart-theme";

import { incomeColor } from "../chart-colors";
import {
  donutSlices,
  incomeShare,
  toIncomeBars,
  type TaxSummary,
} from "../logic";

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

/** Donut + table of income by category. Memo lines (qualified dividends, tax-exempt interest)
 * are listed underneath and are never added to the total. */
export function IncomeBreakdown({ summary }: { summary: TaxSummary }) {
  const bars = React.useMemo(() => toIncomeBars(summary), [summary]);
  const slices = React.useMemo(() => donutSlices(bars), [bars]);
  const total = summary.total_income_minor;
  if (bars.length === 0) {
    return (
      <EmptyState
        title="No income recorded"
        description="No income was found for this year."
      />
    );
  }
  const description = bars
    .map((b) => {
      const share = incomeShare(bars, b);
      return share === null
        ? `${b.label} loss`
        : `${b.label} ${Math.round(share * 100)}%`;
    })
    .join(", ");
  return (
    <div className="flex flex-col gap-4" data-testid="income-breakdown">
      <div className="flex flex-col items-center gap-4 sm:flex-row">
        <div
          className="h-44 w-44 shrink-0"
          role="img"
          aria-label={`Income by category: ${description}`}
        >
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={slices}
                dataKey="value"
                nameKey="label"
                innerRadius="55%"
                outerRadius="95%"
                paddingAngle={slices.length > 1 ? 2 : 0}
                stroke="hsl(var(--card))"
                isAnimationActive={false}
              >
                {slices.map((b) => (
                  <Cell key={b.category} fill={incomeColor(b.category)} />
                ))}
              </Pie>
              <Tooltip
                formatter={(value: number, name: string) => [
                  usd.format(value / 100),
                  name,
                ]}
                {...CHART_TOOLTIP_PROPS}
              />
            </PieChart>
          </ResponsiveContainer>
        </div>
        <table className="w-full text-sm">
          <caption className="sr-only">Income by category</caption>
          <thead>
            <tr className="text-left text-xs text-muted-foreground">
              <th scope="col" className="pb-1 font-medium">
                Category
              </th>
              <th scope="col" className="pb-1 text-right font-medium">
                Amount
              </th>
              <th scope="col" className="pb-1 text-right font-medium">
                Share
              </th>
            </tr>
          </thead>
          <tbody>
            {bars.map((b) => (
              <tr key={b.category} className="border-t border-border">
                <td className="py-1.5">
                  <span
                    aria-hidden="true"
                    className="mr-2 inline-block h-3 w-3 rounded-sm align-middle"
                    style={
                      b.value > 0
                        ? { background: incomeColor(b.category) }
                        : { border: "1px dashed hsl(var(--muted-foreground))" }
                    }
                  />
                  {b.label}
                </td>
                <td className="py-1.5 text-right">
                  <Money tone="neutral" minorUnits={b.value} />
                </td>
                <td className="py-1.5 text-right tabular-nums text-muted-foreground">
                  {(() => {
                    const share = incomeShare(bars, b);
                    return share === null
                      ? "—"
                      : `${(share * 100).toFixed(0)}%`;
                  })()}
                </td>
              </tr>
            ))}
            <tr className="border-t-2 border-border font-medium">
              <td className="py-1.5">Total income</td>
              <td className="py-1.5 text-right">
                <Money tone="neutral" minorUnits={total} />
              </td>
              <td />
            </tr>
          </tbody>
        </table>
      </div>
      {summary.memo_by_category.length > 0 ? (
        <ul
          className="flex flex-col gap-1 text-xs text-muted-foreground"
          data-testid="income-memo"
        >
          {summary.memo_by_category.map((m) => (
            <li key={m.category}>
              Memo: {m.label}{" "}
              <Money tone="neutral" minorUnits={m.amount_minor} /> (memo only;
              not added to the total)
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
