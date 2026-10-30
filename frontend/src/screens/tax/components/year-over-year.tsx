import * as React from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { CHART_INCOME, CHART_TAX, TOOLTIP_STYLE } from "../chart-colors";
import {
  headlineBasisLabel,
  headlineBasisNote,
  yearOverYear,
  yearPointLabel,
  type HeadlineBasis,
  type TaxSummary,
} from "../logic";

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});
const compact = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});

/** Total income vs total tax per year; rendered by the screen only when more than one year exists.
 * Each year's tax has its own basis (return, estimate or withholding only), so years whose federal
 * tax does not come from a return are marked on the axis, drawn lighter and explained under the chart. */
export function YearOverYear({
  summaries,
}: {
  summaries: readonly TaxSummary[];
}) {
  const points = React.useMemo(
    () =>
      yearOverYear(summaries).map((p) => ({ ...p, label: yearPointLabel(p) })),
    [summaries],
  );
  const bases = [
    ...new Set(
      points.flatMap((p) =>
        p.basis !== null && p.basis !== "return" ? [p.basis] : [],
      ),
    ),
  ];
  const axis = {
    stroke: "hsl(var(--muted-foreground))",
    tick: { fill: "hsl(var(--muted-foreground))" },
    fontSize: 12,
  };
  return (
    <div className="flex flex-col gap-2">
      <div
        className="h-64 w-full"
        data-testid="year-over-year"
        role="img"
        aria-label={`Income and tax by year: ${points
          .map(
            (p) =>
              `${p.year} income ${usd.format(p.income / 100)}, tax ${usd.format(p.tax / 100)}${
                p.basis ? ` (${headlineBasisLabel(p.basis)})` : ""
              }`,
          )
          .join("; ")}`}
      >
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={points}
            margin={{ top: 8, right: 8, bottom: 0, left: 8 }}
          >
            <CartesianGrid
              strokeDasharray="3 3"
              vertical={false}
              stroke="hsl(var(--border))"
            />
            <XAxis dataKey="label" {...axis} />
            <YAxis
              tickFormatter={(v: number) => compact.format(v / 100)}
              width={72}
              {...axis}
            />
            <Tooltip
              formatter={(value: number, name: string) => [
                usd.format(value / 100),
                name,
              ]}
              labelFormatter={(label: string, payload) => {
                const basis = (
                  payload?.[0]?.payload as { basis?: string | null } | undefined
                )?.basis;
                return basis
                  ? `${label} · tax ${headlineBasisLabel(basis as HeadlineBasis)}`
                  : label;
              }}
              contentStyle={TOOLTIP_STYLE}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Bar
              dataKey="income"
              name="Total income"
              fill={CHART_INCOME}
              isAnimationActive={false}
            />
            <Bar
              dataKey="tax"
              name="Total tax"
              fill={CHART_TAX}
              isAnimationActive={false}
            >
              {points.map((p) => (
                <Cell
                  key={p.year}
                  fill={CHART_TAX}
                  fillOpacity={
                    p.basis === null || p.basis === "return" ? 1 : 0.5
                  }
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      {bases.length > 0 ? (
        <p
          className="text-xs text-muted-foreground"
          data-testid="year-over-year-note"
        >
          Lighter tax bars are years without a Form 1040:{" "}
          {bases.map((b) => headlineBasisNote(b)).join(" ")}
        </p>
      ) : null}
    </div>
  );
}
