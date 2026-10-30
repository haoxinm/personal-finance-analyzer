import * as React from "react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import type { components } from "@/api/client";
import { Money } from "@/components/primitives/money";
import { EmptyState } from "@/components/primitives/states";

import { NET_WORTH_CATEGORY_COLORS } from "./chart-colors";
import { pieSlices } from "./logic";

type Snapshot = components["schemas"]["NetWorthSnapshot"];

export interface NetWorthPieProps {
  snapshot: Snapshot;
  currency: string;
}

/** Composition of assets (checking / savings / investment). Credit card debt is deliberately
 * not a slice: the pie answers "where is my money held?", not "what do I owe?". The legend
 * list carries the exact numbers so the chart is never the only way to read them. */
export function NetWorthPie({ snapshot, currency }: NetWorthPieProps) {
  const slices = React.useMemo(() => pieSlices(snapshot), [snapshot]);
  const format = React.useMemo(() => {
    const f = new Intl.NumberFormat("en-US", { style: "currency", currency });
    return (minor: number) => f.format(minor / 100);
  }, [currency]);

  if (slices.length === 0) {
    return (
      <EmptyState
        title="No assets to chart"
        description="Checking, savings and investment balances will appear here."
      />
    );
  }

  const summary = slices.map((s) => `${s.label} ${s.percent.toFixed(0)}%`).join(", ");

  return (
    <div className="flex flex-col items-center gap-3 sm:flex-row" data-testid="net-worth-pie">
      <div className="h-44 w-44 shrink-0" role="img" aria-label={`Assets by category: ${summary}`}>
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
              {slices.map((s) => (
                <Cell key={s.category} fill={NET_WORTH_CATEGORY_COLORS[s.category]} />
              ))}
            </Pie>
            <Tooltip
              formatter={(value: number, name: string) => [format(value), name]}
              contentStyle={{
                background: "hsl(var(--card))",
                color: "hsl(var(--card-foreground))",
                border: "1px solid hsl(var(--border))",
                borderRadius: 6,
              }}
              itemStyle={{ color: "hsl(var(--card-foreground))" }}
            />
          </PieChart>
        </ResponsiveContainer>
      </div>
      <ul className="flex w-full flex-col gap-1.5 text-sm">
        {slices.map((s) => (
          <li key={s.category} className="flex items-center justify-between gap-3">
            <span className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className="inline-block h-3 w-3 rounded-sm"
                style={{ background: NET_WORTH_CATEGORY_COLORS[s.category] }}
              />
              {s.label}
            </span>
            <span className="tabular-nums">
              <Money tone="neutral" minorUnits={s.value} currency={currency} />
              <span className="ml-2 text-muted-foreground">{s.percent.toFixed(0)}%</span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
