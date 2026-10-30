/**
 * Chart fill colours. Recharts renders to SVG, which needs concrete colour values rather than
 * the design system's `hsl(var(--token))` custom properties, so this is a small hex palette
 * chosen to echo `CategoryBadge`'s hue set (§ P1-F) without depending on it — a stable hash of
 * the category key picks a colour deterministically, so a category keeps the same colour across
 * charts and sessions.
 */
const PALETTE: readonly string[] = [
  "#e11d48", // rose
  "#d97706", // amber
  "#65a30d", // lime
  "#059669", // emerald
  "#0891b2", // cyan
  "#0284c7", // sky
  "#7c3aed", // violet
  "#c026d3", // fuchsia
  "#4f46e5", // indigo
  "#475569", // slate
  "#ca8a04", // yellow
  "#be185d", // pink
];

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
export function colorMapForKeys(keys: readonly string[]): Map<string, string> {
  const sorted = [...new Set(keys)].sort();
  return new Map(sorted.map((k, i) => [k, colorByIndex(i)]));
}

/** The n-th palette colour (wraps), for series that need distinct colours by position. */
export function colorByIndex(index: number): string {
  return PALETTE[index % PALETTE.length] ?? PALETTE[0]!;
}

export const CHART_OUTFLOW = "#c2410c"; // matches --outflow
export const CHART_INFLOW = "#0f766e"; // matches --inflow
