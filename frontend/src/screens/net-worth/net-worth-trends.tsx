import * as React from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { components } from "@/api/client";
import { EmptyState } from "@/components/primitives/states";
import {
  CHART_AXIS_PROPS,
  CHART_GRID_PROPS,
  CHART_LEGEND_STYLE,
  CHART_LINE_CURSOR,
  CHART_TOOLTIP_PROPS,
} from "@/lib/chart-theme";
import { colorByIndex } from "@/screens/dashboard/chart-colors";
import { cn } from "@/utils";

import { CHART_NET_WORTH, NET_WORTH_CATEGORY_COLORS } from "./chart-colors";
import {
  CATEGORIES,
  CATEGORY_LABELS,
  accountSeriesRows,
  categorySeriesRows,
  periodLabel,
} from "./logic";

type Summary = components["schemas"]["NetWorthSummary"];
type User = components["schemas"]["User"];

export type TrendMode = "category" | "account";

export interface NetWorthTrendsProps {
  summary: Summary;
  users: readonly User[];
  currency: string;
}

interface DotProps {
  cx?: number;
  cy?: number;
  stroke?: string;
  payload?: Record<string, unknown>;
  index?: number;
}

/** Net worth and balances over time, either per category (with a dashed net-worth line) or per
 * account. A hollow dot marks a balance carried forward from an earlier month. */
export function NetWorthTrends({ summary, users, currency }: NetWorthTrendsProps) {
  const [mode, setMode] = React.useState<TrendMode>("category");
  const format = React.useMemo(() => {
    const f = new Intl.NumberFormat("en-US", { style: "currency", currency });
    return (minor: number) => f.format(minor / 100);
  }, [currency]);
  const compact = React.useMemo(() => {
    const f = new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
      notation: "compact",
      maximumFractionDigits: 1,
    });
    return (minor: number) => f.format(minor / 100);
  }, [currency]);

  const categoryRows = React.useMemo(() => categorySeriesRows(summary.series), [summary.series]);
  const account = React.useMemo(
    () => accountSeriesRows(summary.account_series, users),
    [summary.account_series, users],
  );

  const options: Array<{ value: TrendMode; label: string }> = [
    { value: "category", label: "By category" },
    { value: "account", label: "By account" },
  ];

  const toggle = (
    <div
      role="radiogroup"
      aria-label="Chart grouping"
      className="inline-flex gap-1 rounded-full bg-muted p-1"
    >
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={mode === o.value}
          onClick={() => setMode(o.value)}
          className={cn(
            "rounded-full border px-3 py-1 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            mode === o.value
              ? "border-primary bg-primary text-primary-foreground shadow-sm"
              : "border-transparent text-muted-foreground hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );

  if (summary.series.length === 0) {
    return (
      <div className="flex flex-col gap-3">
        {toggle}
        <EmptyState title="No trend yet" description="Add balances for a few months to see them over time." />
      </div>
    );
  }

  const common = (
    <>
      <CartesianGrid {...CHART_GRID_PROPS} />
      <XAxis dataKey="period" tickFormatter={periodLabel} {...CHART_AXIS_PROPS} />
      <YAxis tickFormatter={(v: number) => compact(v)} width={72} {...CHART_AXIS_PROPS} />
      <Legend wrapperStyle={CHART_LEGEND_STYLE} />
    </>
  );
  const singlePeriod = summary.series.length === 1;

  return (
    <div className="flex flex-col gap-3">
      {toggle}
      {mode === "category" ? (
        <div className="h-72 w-full" data-testid="net-worth-trend-category">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={categoryRows} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
              {common}
              <Tooltip
                formatter={(value: number, name: string) => [format(value), name]}
                labelFormatter={(label: string) => periodLabel(label)}
                {...CHART_TOOLTIP_PROPS}
                cursor={CHART_LINE_CURSOR}
              />
              {CATEGORIES.map((c) => (
                <Line
                  key={c}
                  type="monotone"
                  dataKey={c}
                  name={CATEGORY_LABELS[c]}
                  stroke={NET_WORTH_CATEGORY_COLORS[c]}
                  strokeWidth={2}
                  dot={{ r: 3 }}
                  isAnimationActive={false}
                />
              ))}
              <Line
                type="monotone"
                dataKey="net_worth"
                name="Net worth"
                stroke={CHART_NET_WORTH}
                strokeWidth={3}
                strokeDasharray="6 3"
                dot={{ r: 4 }}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      ) : account.keys.length === 0 ? (
        <EmptyState title="No accounts yet" description="Account balances will appear here." />
      ) : (
        <div className="h-72 w-full" data-testid="net-worth-trend-account">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={account.rows} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
              {common}
              <Tooltip
                formatter={(value: number, name: string) => [format(value), name]}
                labelFormatter={(label: string) => periodLabel(label)}
                {...CHART_TOOLTIP_PROPS}
                cursor={CHART_LINE_CURSOR}
              />
              {account.keys.map((k, i) => {
                const color = colorByIndex(i);
                return (
                  <Line
                    key={k.key}
                    type="monotone"
                    dataKey={k.key}
                    name={k.label}
                    stroke={color}
                    strokeWidth={2}
                    connectNulls={false}
                    isAnimationActive={false}
                    dot={(props: DotProps) => {
                      const { cx, cy, payload, index } = props;
                      if (cx === undefined || cy === undefined) return <g key={`d-${k.key}-${index}`} />;
                      const carried = payload?.[`carried:${k.key}`] === true;
                      return (
                        <circle
                          key={`d-${k.key}-${index}`}
                          cx={cx}
                          cy={cy}
                          r={4}
                          stroke={color}
                          strokeWidth={2}
                          fill={carried ? "hsl(var(--card))" : color}
                        />
                      );
                    }}
                  />
                );
              })}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        {singlePeriod ? "Add another month to see a trend. " : ""}
        {mode === "account" ? "Hollow dots are balances carried forward from an earlier month. " : ""}
        {mode === "category" ? "Credit card is the amount owed; net worth subtracts it." : ""}
      </p>
    </div>
  );
}
