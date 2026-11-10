import type { components } from "@/api/client";
import type { JobEvent } from "@/hooks/use-job-progress";

export type Statement = components["schemas"]["Statement"];
export type ExtractorProposal = components["schemas"]["ExtractorProposal"];
export type Issuer = components["schemas"]["Issuer"];
export type LayoutSpec = components["schemas"]["LayoutSpec"];

/** Which extractor an item will run with: a built-in layout parser (issuer-agnostic, scored by
 * `detect()`) or a specific, versioned layout spec (§2f.1, tied to an issuer). Exactly one of
 * these travels in `ExtractRequest`. */
export type ExtractorChoice =
  | { kind: "parser"; parserId: string }
  | { kind: "spec"; layoutSpecId: number };

/** The screen's per-file state machine. `clientId` is a locally generated id so a file can be
 * tracked from the moment it is dropped, before the server has assigned a `statement_id`. */
export type ImportItemStatus =
  | "uploading"
  | "upload_failed"
  | "awaiting_extractor"
  | "extracting"
  | "parsed"
  | "no_text_layer"
  | "unsupported_layout"
  | "error";

export interface ImportItem {
  clientId: string;
  fileName: string;
  userId: number;
  status: ImportItemStatus;

  statementId?: number;
  statement?: Statement;
  proposal?: ExtractorProposal;

  issuerId: number | null;
  extractorChoice: ExtractorChoice | null;
  remember: boolean;

  /** Pages to extract from: `null`/undefined = all pages. An empty array means the user has
   * deselected everything (importing is blocked until at least one page is chosen). */
  selectedPages?: number[] | null;

  jobId?: string;
  jobEvent?: JobEvent;

  /** Best-effort count of transactions this import flagged `needs_review` (§3.12). Undefined
   * while unknown (not attempted, or the attempt found nothing to go on yet) — the summary line
   * sums only the items that have one. See `fetchNeedsReviewCount` for the known limitation. */
  needsReviewCount?: number;

  /** The upload was a byte-identical file already imported (`StatementUploadResponse.duplicate`):
   * `statement` is the earlier, parsed one, so nothing new will be added by importing again. */
  duplicate?: boolean;

  /** From the finished import job: rows newly added, and rows skipped because the same transaction
   * was already present (an overlapping statement). Undefined until the job's counts are read. */
  insertedCount?: number;
  skippedDuplicates?: number;

  errorMessage?: string;

  /** Whether the extractor of the in-flight/last import attempt was the LLM (decides the wording
   * of a failure warning and whether to preselect the LLM afterwards). */
  attemptedLlm?: boolean;

  /** A failed parse is a warning, not a dead end (the staged PDF is kept server-side): the item
   * goes back to `awaiting_extractor` with the failure detail here, the picker still available. */
  failure?: { detail: string; llm: boolean; previous?: boolean };
}
