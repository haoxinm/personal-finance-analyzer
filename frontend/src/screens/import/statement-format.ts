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

/** The reason shown (and used as the button's description) when the kept PDF is gone. */
export const PDF_MISSING_REASON =
  "PDF not found in your documents folder. Put it back, or delete the statement and drop the PDF again.";

/** Server wording of the shape warning a 0-row parse carries; shown once, as its own note. */
const NO_ROWS_WARNING_PREFIX = "No transactions were found";

export function parseShapeWarnings(raw: string | null | undefined): string[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((w): w is string => typeof w === "string") : [];
  } catch {
    return [];
  }
}

export interface AttentionNote {
  code: "no_transactions" | "all_duplicates" | "layout_drift" | "pdf_missing";
  tone: "warn" | "info";
  text: string;
}

/** Whether a parsed statement produced no rows at all (the wrong extractor or pages). */
export function hasNoTransactions(statement: Statement | undefined | null): boolean {
  return statement?.status === "parsed" && (statement.attention ?? []).includes("no_transactions");
}

/** The "needs attention" lines of a parsed statement, from the server's `attention` flags. The
 * reconciliation mismatch is not here: it carries a formatted amount, so its owner renders it. */
export function attentionNotes(statement: Statement): AttentionNote[] {
  if (statement.status !== "parsed") return [];
  const flags = new Set(statement.attention ?? []);
  const notes: AttentionNote[] = [];
  if (flags.has("no_transactions")) {
    notes.push({
      code: "no_transactions",
      tone: "warn",
      text: "No transactions were found. The wrong extractor or pages were probably used.",
    });
  }
  if (flags.has("all_duplicates")) {
    const n = statement.parsed_row_count ?? 0;
    notes.push({
      code: "all_duplicates",
      tone: "info",
      text: `All ${plural(n, "transaction")} ${n === 1 ? "was" : "were"} already on file from another statement.`,
    });
  }
  if (flags.has("layout_drift")) {
    for (const warning of parseShapeWarnings(statement.shape_warnings)) {
      if (flags.has("no_transactions") && warning.startsWith(NO_ROWS_WARNING_PREFIX)) continue;
      notes.push({ code: "layout_drift", tone: "warn", text: warning });
    }
  }
  if (flags.has("pdf_missing")) notes.push({ code: "pdf_missing", tone: "warn", text: PDF_MISSING_REASON });
  return notes;
}

/** What a finished re-import did, in plain words: the headline plus anything the user must know
 * about their edits (unmatched ones were dropped; discarded ones were asked for). */
export function describeReimport(job: Job): { headline: string; notes: string[] } {
  const inserted = job.inserted ?? 0;
  const restored = job.restored_edits ?? 0;
  const unmatched = job.unmatched_edits ?? 0;
  const discarded = job.discarded_edits ?? 0;
  const was = job.previous_txn_count != null && job.previous_txn_count !== inserted ? ` (was ${job.previous_txn_count})` : "";
  const parts = [`Re-imported: ${plural(inserted, "transaction")}${was}.`];
  if (restored > 0) parts.push(`${plural(restored, "edit")} kept.`);
  const notes: string[] = [];
  if (inserted === 0 && job.parsed_count === 0) notes.push("No transactions were found.");
  if (unmatched > 0) {
    notes.push(
      `${plural(unmatched, "edit")} could not be matched to a transaction and ${unmatched === 1 ? "was" : "were"} dropped.`,
    );
  }
  if (discarded > 0) notes.push(`${plural(discarded, "edit")} discarded, as you asked.`);
  if ((job.skipped_duplicates ?? 0) > 0) {
    notes.push(`${plural(job.skipped_duplicates ?? 0, "transaction")} already on file from another statement.`);
  }
  for (const warning of job.warnings ?? []) {
    if (warning.startsWith(NO_ROWS_WARNING_PREFIX)) continue;
    notes.push(warning);
  }
  return { headline: parts.join(" "), notes };
}

/** Whether two statements' periods overlap (both need a known period). */
export function periodsOverlap(a: Statement, b: Statement): boolean {
  if (!a.period_start || !a.period_end || !b.period_start || !b.period_end) return false;
  return a.period_start <= b.period_end && b.period_start <= a.period_end;
}

/** The transactions grid for one statement, with every kind (payments and transfers too, not only
 * spending) so the row count matches the statement's. */
export function statementTransactionsUrl(statementId: number | string): string {
  return `/spending/transactions?statement_id=${statementId}&include_non_spend=true`;
}

/** A human name for a recorded extractor: the LLM, or a built-in layout parser by its id with the
 * underscores read as spaces. */
export function extractorDisplayName(parserId: string | null | undefined): string {
  if (!parserId) return "the recorded extractor";
  if (parserId === "llm") return "LLM extractor";
  return `built-in parser ${parserId.replace(/_/g, " ")}`;
}
