import * as React from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  CHART_AXIS_PROPS,
  CHART_GRID_PROPS,
  CHART_LEGEND_STYLE,
  CHART_LINE_CURSOR,
  CHART_TOOLTIP_PROPS,
} from "@/lib/chart-theme";
import { CHART_NET_WORTH } from "../chart-colors";
import type { DollarMode, ForecastRow } from "./forecast-logic";
import { chartData } from "./chart-data";
import { formatCompactUsd, formatUsd, type HistoryRow } from "./params";

export interface ForecastChartProps {
  history: readonly HistoryRow[];
  /** Projected rows in the dollars of `mode`. */
  rows: readonly ForecastRow[];
  baseYear: number;
  /** Net worth at `baseYear` (the point the dashed line starts from). */
  startNetWorthMinor: number;
  targetYear: number;
  mode: DollarMode;
}

/** Net worth over time: history as a solid line, the projection dashed, the target year marked. */
export function ForecastChart({ history, rows, baseYear, startNetWorthMinor, targetYear, mode }: ForecastChartProps) {
  const data = React.useMemo(
    () => chartData(history, rows, baseYear, startNetWorthMinor),
    [history, rows, baseYear, startNetWorthMinor],
  );
  const last = rows[rows.length - 1];
  const label = `Net worth by year, ${data[0]?.year ?? baseYear} to ${targetYear}${
    last ? `; projected ${formatUsd(last.netWorth)} in ${last.year}` : ""
  } (${mode === "real" ? `${baseYear} dollars` : "nominal dollars"}).`;

  return (
    <div className="h-80 w-full" role="img" aria-label={label} data-testid="forecast-chart">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
          <CartesianGrid {...CHART_GRID_PROPS} />
          <XAxis
            dataKey="year"
            type="number"
            domain={["dataMin", "dataMax"]}
            allowDecimals={false}
            tickFormatter={(v: number) => String(v)}
            {...CHART_AXIS_PROPS}
          />
          <YAxis tickFormatter={(v: number) => formatCompactUsd(v)} width={72} {...CHART_AXIS_PROPS} />
          <Legend wrapperStyle={CHART_LEGEND_STYLE} />
          <Tooltip
            formatter={(value: number, name: string) => [formatUsd(value), name]}
            labelFormatter={(year: number) => String(year)}
            {...CHART_TOOLTIP_PROPS}
            cursor={CHART_LINE_CURSOR}
          />
          <ReferenceLine
            x={targetYear}
            stroke="hsl(var(--muted-foreground))"
            strokeDasharray="2 4"
            label={{ value: `Target ${targetYear}`, position: "insideTopRight", fontSize: 12, fill: "hsl(var(--muted-foreground))" }}
          />
          <Line
            type="monotone"
            dataKey="history"
            name={mode === "real" ? "Actual (nominal dollars)" : "Actual"}
            stroke={CHART_NET_WORTH}
            strokeWidth={3}
            dot={{ r: 3 }}
            connectNulls
            isAnimationActive={false}
          />
          <Line
            type="monotone"
            dataKey="projection"
            name={mode === "real" ? `Projected (${baseYear} dollars)` : "Projected"}
            stroke={CHART_NET_WORTH}
            strokeWidth={3}
            strokeDasharray="6 4"
            dot={false}
            connectNulls
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
