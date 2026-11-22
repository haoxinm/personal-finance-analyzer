import type { components } from "@/api/client";

type Statement = components["schemas"]["Statement"];
type Job = components["schemas"]["Job"];

export const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function formatDay(iso: string, withYear: boolean): string {
  const date = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(withYear ? { year: "numeric" } : {}) });
}

/** "Mar 1 – Mar 31, 2026". */
export function formatPeriod(start: string | null, end: string | null): string {
  if (!start && !end) return "Period unknown";
  if (!start || !end) return formatDay((start ?? end) as string, true);
  const sameYear = start.slice(0, 4) === end.slice(0, 4);
  return `${formatDay(start, !sameYear)} – ${formatDay(end, true)}`;
}

/** Newest period first; statements without a period last, newest upload first within a tie. */
export function sortStatements(rows: readonly Statement[]): Statement[] {
  return [...rows].sort((a, b) => {
    const byPeriod = (b.period_end ?? "").localeCompare(a.period_end ?? "");
    return byPeriod !== 0 ? byPeriod : b.created_at.localeCompare(a.created_at);
  });
}

/** What a finished re-import did, in plain words: the headline plus anything the user must know
 * about their edits (unmatched ones were dropped; discarded ones were asked for). */
export function describeReimport(job: Job): { headline: string; notes: string[] } {
  const inserted = job.inserted ?? 0;
  const restored = job.restored_edits ?? 0;
  const unmatched = job.unmatched_edits ?? 0;
  const discarded = job.discarded_edits ?? 0;
  const parts = [`Re-imported: ${plural(inserted, "transaction")}.`];
  if (restored > 0) parts.push(`${plural(restored, "edit")} kept.`);
  const notes: string[] = [];
  if (unmatched > 0) {
    notes.push(
      `${plural(unmatched, "edit")} could not be matched to a transaction and ${unmatched === 1 ? "was" : "were"} dropped.`,
    );
  }
  if (discarded > 0) notes.push(`${plural(discarded, "edit")} discarded, as you asked.`);
  if ((job.skipped_duplicates ?? 0) > 0) {
    notes.push(`${plural(job.skipped_duplicates ?? 0, "transaction")} already on file from another statement.`);
  }
  return { headline: parts.join(" "), notes };
}

