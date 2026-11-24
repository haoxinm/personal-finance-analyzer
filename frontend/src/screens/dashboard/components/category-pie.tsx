import * as React from "react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { Money } from "@/components/primitives/money";
import { EmptyState } from "@/components/primitives/states";
import { CHART_TOOLTIP_PROPS } from "@/lib/chart-theme";

import { colorMapForKeys } from "../chart-colors";
import {
  OTHER_KEY,
  defaultCategoryLabel,
  pieSlicesFromTotals,
  totalsByCategory,
  type AnalyticsRow,
} from "../logic";

export interface CategoryPieProps {
  rows: readonly AnalyticsRow[];
  currency: string;
  colors?: Map<string, string>;
  labelFor?: (key: string) => string;
  /** Makes slices and legend rows clickable (the merged "All other categories" one never is). */
  onSelectCategory?: (key: string) => void;
}

/** Share of spending per category across the whole range (a pie can't show time). The 8 largest
 * categories get a slice, the rest merge into "Other", and categories whose refunds exceed their
 * spending are listed underneath rather than silently dropped. */
export function CategoryPie({
  rows,
  currency,
  colors: sharedColors,
  labelFor = defaultCategoryLabel,
  onSelectCategory,
}: CategoryPieProps) {
  const totals = React.useMemo(() => totalsByCategory(rows), [rows]);
  const { slices, excluded, totalMinor } = React.useMemo(() => pieSlicesFromTotals(totals, 8), [totals]);
  const ownColors = React.useMemo(() => colorMapForKeys(totals.map((t) => t.key), true), [totals]);
  const colors = sharedColors ?? ownColors;
  const format = React.useMemo(() => {
    const f = new Intl.NumberFormat("en-US", { style: "currency", currency });
    return (minor: number) => f.format(minor / 100);
  }, [currency]);

  const membersOf = (key: string) => slices.find((s) => s.key === key)?.members;
  const canSelect = (key: string) => onSelectCategory !== undefined && key !== OTHER_KEY;

  const colorOf = (key: string) => (key === OTHER_KEY ? "hsl(var(--muted-foreground))" : colors.get(key));

  if (slices.length === 0) {
    return (
      <div className="flex flex-col gap-3" data-testid="category-pie">
        <EmptyState
          title="No spending to chart"
          description="Categories need net spending above zero to appear in a pie."
        />
        {excluded.length > 0 ? <ExcludedNote keys={excluded} labelFor={labelFor} /> : null}
      </div>
    );
  }

  const summary = slices.map((s) => `${labelFor(s.key)} ${s.percent.toFixed(0)}%`).join(", ");
  const data = slices.map((s) => ({ ...s, label: labelFor(s.key) }));

  return (
    <div className="flex flex-col gap-3" data-testid="category-pie">
      <div className="flex flex-col items-center gap-4 sm:flex-row">
        <div
          className="relative h-56 w-56 shrink-0"
          role="img"
          aria-label={`Spending by category: ${summary}`}
        >
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={data}
                dataKey="totalMinor"
                nameKey="label"
                innerRadius="55%"
                outerRadius="95%"
                paddingAngle={slices.length > 1 ? 2 : 0}
                stroke="hsl(var(--card))"
                isAnimationActive={false}
              >
                {slices.map((s) => (
                  <Cell
                    key={s.key}
                    fill={colorOf(s.key)}
                    cursor={canSelect(s.key) ? "pointer" : undefined}
                    onClick={canSelect(s.key) ? () => onSelectCategory?.(s.key) : undefined}
                  />
                ))}
              </Pie>
              <Tooltip
                formatter={(value: number, name: string, item) => {
                  const members = membersOf((item?.payload as { key?: string } | undefined)?.key ?? "");
                  return [format(value), members ? `${name}: ${members.map((m) => labelFor(m)).join(", ")}` : name];
                }}
                {...CHART_TOOLTIP_PROPS}
              />
            </PieChart>
          </ResponsiveContainer>
          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
            <span className="text-xs text-muted-foreground">Total</span>
            <Money tone="neutral" minorUnits={totalMinor} currency={currency} className="text-base font-semibold" />
          </div>
        </div>
        <ul className="flex w-full min-w-0 max-w-md flex-col gap-1.5 text-sm">
          {slices.map((s) => (
            <li key={s.key} className="flex items-center justify-between gap-3">
              {(() => {
                const label = (
                  <>
                    <span
                      aria-hidden="true"
                      className="inline-block h-3 w-3 shrink-0 rounded-sm"
                      style={{ background: colorOf(s.key) }}
                    />
                    <span className="truncate">{labelFor(s.key)}</span>
                  </>
                );
                const members = s.members;
                if (canSelect(s.key)) {
                  return (
                    <button
                      type="button"
                      className="flex min-w-0 items-center gap-2 rounded-sm text-left hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      title={`Show ${labelFor(s.key)} transactions`}
                      onClick={() => onSelectCategory?.(s.key)}
                    >
                      {label}
                    </button>
                  );
                }
                return (
                  <span
                    className="flex min-w-0 items-center gap-2"
                    title={members ? `Includes: ${members.map((m) => labelFor(m)).join(", ")}` : undefined}
                  >
                    {label}
                  </span>
                );
              })()}
              <span className="shrink-0 tabular-nums">
                <Money tone="neutral" minorUnits={s.totalMinor} currency={currency} />
                <span className="ml-2 inline-block w-10 text-right text-muted-foreground">
                  {s.percent.toFixed(0)}%
                </span>
              </span>
            </li>
          ))}
        </ul>
      </div>
      {excluded.length > 0 ? <ExcludedNote keys={excluded} labelFor={labelFor} /> : null}
    </div>
  );
}

function ExcludedNote({ keys, labelFor }: { keys: string[]; labelFor: (key: string) => string }) {
  return (
    <p className="text-xs text-muted-foreground">
      Not shown (refunds exceed spending): {keys.map((k) => labelFor(k)).join(", ")}
    </p>
  );
}
