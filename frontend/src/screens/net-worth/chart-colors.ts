/** Net-worth category colours: fixed per category (not hashed) so the pie, the trend lines and
 * the breakdown swatches always agree. Credit card shares no hue with the three asset hues. */
export const NET_WORTH_CATEGORY_COLORS: Record<string, string> = {
  checking: "hsl(var(--chart-sky))",
  savings: "hsl(var(--chart-emerald))",
  investment: "hsl(var(--chart-violet))",
  credit_card: "hsl(var(--chart-rose))",
};
export const CHART_NET_WORTH = "hsl(var(--chart-teal))";
