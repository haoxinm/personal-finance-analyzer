/**
 * Shared Recharts props so every chart reads in both themes. All colours are `hsl(var(--token))`
 * strings: Recharts writes them into SVG attributes/inline styles where `var()` resolves, so a
 * theme switch recolours charts without re-rendering.
 */
export const CHART_AXIS_PROPS = {
  stroke: "hsl(var(--border))",
  tick: { fill: "hsl(var(--muted-foreground))", fontSize: 12 },
  tickLine: { stroke: "hsl(var(--border))" },
} as const;

export const CHART_GRID_PROPS = {
  strokeDasharray: "3 3",
  vertical: false,
  stroke: "hsl(var(--border))",
} as const;

export const CHART_TOOLTIP_PROPS = {
  contentStyle: {
    background: "hsl(var(--card))",
    color: "hsl(var(--card-foreground))",
    border: "1px solid hsl(var(--border))",
    borderRadius: 6,
    fontSize: 12,
  },
  itemStyle: { color: "hsl(var(--card-foreground))" },
  labelStyle: { color: "hsl(var(--muted-foreground))" },
  cursor: { fill: "hsl(var(--muted))", fillOpacity: 0.6 },
} as const;

/** Tooltip cursor for line charts (a dashed vertical guide instead of a filled band). */
export const CHART_LINE_CURSOR = {
  stroke: "hsl(var(--muted-foreground))",
  strokeDasharray: "3 3",
} as const;

export const CHART_LEGEND_STYLE = {
  fontSize: 12,
  color: "hsl(var(--muted-foreground))",
} as const;
