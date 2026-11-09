import type { TaxParameters } from "./hooks";

/** "2022-2024, 2026" for the covered years: runs of consecutive years collapse to a range. */
export function coveredYearsLabel(years: readonly number[]): string {
  const sorted = [...new Set(years)].sort((a, b) => a - b);
  if (sorted.length === 0) return "none";
  const parts: string[] = [];
  let start = sorted[0]!;
  let prev = start;
  const flush = () => parts.push(start === prev ? String(start) : `${start}-${prev}`);
  for (const year of sorted.slice(1)) {
    if (year === prev + 1) {
      prev = year;
      continue;
    }
    flush();
    start = prev = year;
  }
  flush();
  return parts.join(", ");
}

/** Which copy of the parameter file is in effect, as a phrase. */
export function parameterSourceLabel(source: TaxParameters["source"]): string {
  switch (source) {
    case "bundled":
      return "Bundled with the app";
    case "cached":
      return "Downloaded update";
    case "override":
      return "Your own file";
  }
}

/** "Mar 1, 2026" for the server's date-only `last_checked` ("2026-03-01"), "Never" when absent. */
export function lastCheckedLabel(lastChecked: string | null): string {
  if (!lastChecked) return "Never";
  const date = new Date(`${lastChecked.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return lastChecked;
  return date.toLocaleDateString("en-US", { dateStyle: "medium", timeZone: "UTC" });
}

const CHECK_OUTCOME_LABELS: Record<string, string> = {
  updated: "Added new tax years",
  current: "No new tax years",
  rejected: "Download rejected (failed validation)",
  unreachable: "Could not reach GitHub",
  pending: "Check started, not finished",
};

/** The stored result of the last update check as a phrase ("—" when none; unknown values pass through). */
export function checkOutcomeLabel(outcome: string | null): string {
  if (!outcome) return "—";
  return CHECK_OUTCOME_LABELS[outcome] ?? outcome;
}

/** The versions of each file found, e.g. "bundled 3, downloaded 4". */
export function versionsLabel(p: TaxParameters): string {
  const parts = [`bundled ${p.bundled_version}`];
  if (p.cached_version !== null) parts.push(`downloaded ${p.cached_version}`);
  if (p.override_version !== null) parts.push(`your file ${p.override_version}`);
  return parts.join(", ");
}
