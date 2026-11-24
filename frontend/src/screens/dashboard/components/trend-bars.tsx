import * as React from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { EmptyState } from "@/components/primitives/states";
import { CHART_AXIS_PROPS, CHART_GRID_PROPS, CHART_TOOLTIP_PROPS } from "@/lib/chart-theme";

import { CHART_OUTFLOW } from "../chart-colors";
import { toTrendPoints, type AnalyticsRow } from "../logic";

export interface TrendBarsProps {
  rows: readonly AnalyticsRow[];
  currency: string;
}

/** The overall spend trend as one bar per period. A net-negative period (refunds exceed
 * spending) draws below the axis. */
export function TrendBars({ rows, currency }: TrendBarsProps) {
  const points = React.useMemo(() => toTrendPoints(rows), [rows]);
  const format = React.useMemo(() => {
    const formatter = new Intl.NumberFormat("en-US", { style: "currency", currency });
    return (minorUnits: number) => formatter.format(minorUnits / 100);
  }, [currency]);

  if (points.length === 0) {
    return (
      <EmptyState title="No trend yet" description="Spending totals will appear here once classified." />
    );
  }

  return (
    <div
      className="h-56 w-full"
      data-testid="trend-bars"
      role="img"
      aria-label={`Bar chart of total spending: ${points.length} ${points.length === 1 ? "period" : "periods"}`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
          <CartesianGrid {...CHART_GRID_PROPS} />
          <XAxis dataKey="period" {...CHART_AXIS_PROPS} />
          <YAxis tickFormatter={(v: number) => format(v)} width={80} {...CHART_AXIS_PROPS} />
          <Tooltip
            {...CHART_TOOLTIP_PROPS}
            formatter={(value: number) => [format(value), "Total"]}
            labelFormatter={(label: string) => `Period: ${label}`}
          />
          <Bar dataKey="totalMinor" name="Total" fill={CHART_OUTFLOW} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
