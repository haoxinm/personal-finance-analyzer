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

import { EmptyState } from "@/components/primitives/states";
import {
  CHART_AXIS_PROPS,
  CHART_GRID_PROPS,
  CHART_LEGEND_STYLE,
  CHART_LINE_CURSOR,
  CHART_TOOLTIP_PROPS,
} from "@/lib/chart-theme";

import { colorMapForKeys } from "../chart-colors";
import {
  OTHER_KEY,
  defaultCategoryLabel,
  distinctCategories,
  pivotByCategory,
  topCategoriesWithOther,
  type AnalyticsRow,
} from "../logic";

export interface CategoryLinesProps {
  rows: readonly AnalyticsRow[];
  currency: string;
  colors?: Map<string, string>;
  labelFor?: (key: string) => string;
}

/** One line per category over time: the 8 largest categories plus an "Other" line for the rest
 * (a dozen lines is unreadable). Periods a category has no spend in count as 0. */
export function CategoryLines({
  rows,
  currency,
  colors: sharedColors,
  labelFor = defaultCategoryLabel,
}: CategoryLinesProps) {
  const points = React.useMemo(() => pivotByCategory(rows), [rows]);
  const categories = React.useMemo(() => distinctCategories(points), [points]);
  const { keys, data } = React.useMemo(() => topCategoriesWithOther(points, categories, 8), [points, categories]);
  const ownColors = React.useMemo(() => colorMapForKeys(categories, true), [categories]);
  const colors = sharedColors ?? ownColors;
  const format = React.useMemo(() => {
    const formatter = new Intl.NumberFormat("en-US", { style: "currency", currency });
    return (minorUnits: number) => formatter.format(minorUnits / 100);
  }, [currency]);

  if (points.length === 0) {
    return (
      <EmptyState
        title="No spending yet"
        description="Import and classify a statement to see spending by category over time."
      />
    );
  }

  return (
    <div
      className="h-72 w-full"
      data-testid="category-lines"
      role="img"
      aria-label={`Line chart of spending by category over time: ${points.length} ${
        points.length === 1 ? "period" : "periods"
      }, ${categories.length} ${categories.length === 1 ? "category" : "categories"}`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
          <CartesianGrid {...CHART_GRID_PROPS} />
          <XAxis dataKey="period" {...CHART_AXIS_PROPS} />
          <YAxis tickFormatter={(v: number) => format(v)} width={80} {...CHART_AXIS_PROPS} />
          <Tooltip
            {...CHART_TOOLTIP_PROPS}
            cursor={CHART_LINE_CURSOR}
            formatter={(value: number, name: string) => [format(value), name]}
            labelFormatter={(label: string) => `Period: ${label}`}
          />
          <Legend wrapperStyle={CHART_LEGEND_STYLE} />
          {keys.map((key) => {
            const color = key === OTHER_KEY ? "hsl(var(--muted-foreground))" : colors.get(key);
            return (
              <Line
                key={key}
                type="monotone"
                dataKey={key}
                name={labelFor(key)}
                stroke={color}
                strokeWidth={2}
                dot={{ r: 2 }}
                isAnimationActive={false}
              />
            );
          })}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
