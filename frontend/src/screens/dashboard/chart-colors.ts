/**
 * Chart fill colours, as `hsl(var(--chart-*))` strings. Recharts writes them into SVG attributes
 * where `var()` resolves, and the tokens (index.css) have light and dark values, so a theme switch
 * recolours charts with no re-render. The hues echo `CategoryBadge`'s set (§ P1-F) without
 * depending on it — a stable hash of the category key picks a colour deterministically, so a
 * category keeps the same colour across charts and sessions.
 */
const PALETTE: readonly string[] = [
  "hsl(var(--chart-rose))",
  "hsl(var(--chart-amber))",
  "hsl(var(--chart-lime))",
  "hsl(var(--chart-emerald))",
  "hsl(var(--chart-cyan))",
  "hsl(var(--chart-sky))",
  "hsl(var(--chart-violet))",
  "hsl(var(--chart-fuchsia))",
  "hsl(var(--chart-indigo))",
  "hsl(var(--chart-slate))",
  "hsl(var(--chart-yellow))",
  "hsl(var(--chart-pink))",
];

const NEUTRAL = "hsl(var(--chart-slate))";

function hashKey(key: string): number {
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) {
    hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  }
  return hash;
}

export function colorForKey(key: string): string {
  return PALETTE[hashKey(key) % PALETTE.length] ?? PALETTE[0]!;
}

/**
 * Distinct colours for a set of series: keys are ordered alphabetically (so the assignment does
 * not depend on data order and is stable for a given set of categories) and take consecutive
 * palette slots, so no two series collide until the palette is exhausted.
 */
export function colorMapForKeys(keys: readonly string[], avoidNeutral = false): Map<string, string> {
  const sorted = [...new Set(keys)].sort();
  if (avoidNeutral) {
    // Keep the grey slate free for the merged "Other" series/slice of the overview charts.
    const palette = PALETTE.filter((c) => c !== NEUTRAL);
    return new Map(sorted.map((k, i) => [k, palette[i % palette.length] ?? palette[0]!]));
  }
  return new Map(sorted.map((k, i) => [k, colorByIndex(i)]));
}

/** The n-th palette colour (wraps), for series that need distinct colours by position. */
export function colorByIndex(index: number): string {
  return PALETTE[index % PALETTE.length] ?? PALETTE[0]!;
}

export const CHART_OUTFLOW = "hsl(var(--outflow))";
export const CHART_INFLOW = "hsl(var(--inflow))";
