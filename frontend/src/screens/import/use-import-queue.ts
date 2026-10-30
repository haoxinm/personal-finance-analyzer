import type { QueryClient } from "@tanstack/react-query";
import * as React from "react";

import type { JobEvent } from "@/hooks/use-job-progress";

import { fetchNeedsReviewCount, fetchStatement, pollClassifyJobNeedsReview } from "./api";
import { isSkipConfirmForIssuer } from "./storage";
import type { ExtractorChoice, ImportItem, ImportItemStatus, LayoutSpec, Statement } from "./types";

/** A parser/spec is a "confident" proposal (§2f.2: "the parser's `detect_score` is >= 0.8")
 * worth pre-selecting and importable in one click. Mirrors the backend's own
 * `CONFIDENT_SCORE_THRESHOLD` (`ingest/pipeline.py`) so the UI's definition of "confident"
 * matches the one that already decided whether to resolve an issuer at all. */
export const CONFIDENT_SCORE_THRESHOLD = 0.8;

function newClientId(): string {
  return `import-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/** The extractor a fresh proposal implies, before any user override. */
export function extractorChoiceFromProposal(proposal: {
  layout_spec_id: number | null;
  parser_id: string | null;
}): ExtractorChoice | null {
  if (proposal.layout_spec_id !== null) {
    return { kind: "spec", layoutSpecId: proposal.layout_spec_id };
  }
  if (proposal.parser_id !== null) {
    return { kind: "parser", parserId: proposal.parser_id };
  }
  return null;
}

/** The extractor to fall back to when the user switches issuer: that issuer's own approved
 * layout spec if it has one, else the original built-in parser guess (layout detection is
 * issuer-agnostic), else nothing (the user must pick from that issuer's specs, if any). */
export function extractorChoiceForIssuer(
  issuerId: number,
  specs: LayoutSpec[],
  fallbackParserId: string | null,
): ExtractorChoice | null {
  const ownSpec = specs.find((s) => s.issuer_id === issuerId && s.approved);
  if (ownSpec) return { kind: "spec", layoutSpecId: ownSpec.id };
  if (fallbackParserId !== null) return { kind: "parser", parserId: fallbackParserId };
  return null;
}

function statusFromStatement(statement: Statement): ImportItemStatus {
  switch (statement.status) {
    case "parsed":
      return "parsed";
    case "no_text_layer":
      return "no_text_layer";
    case "unsupported_layout":
      return "unsupported_layout";
    case "error":
      return "error";
    default:
      return "awaiting_extractor";
  }
}

const LLM_CHOICE: ExtractorChoice = { kind: "parser", parserId: "llm" };

/** Applies a (re)loaded statement to `item`. A statement that failed to parse — or one re-proposed
 * after such a failure, which the server reopens as `awaiting_extractor` with its `error_detail`
 * kept — is a retryable warning: the item stays importable, and after a built-in failure the LLM
 * is preselected when one is configured (it is never *run* without the user clicking Import). */
function applyStatement(item: ImportItem, statement: Statement, llmConfigured: boolean): ImportItem {
  const failed =
    statement.status === "unsupported_layout" ||
    statement.status === "error" ||
    (statement.status === "awaiting_extractor" && Boolean(statement.error_detail));
  if (!failed) {
    return {
      ...item,
      statement,
      status: statusFromStatement(statement),
      errorMessage: statement.error_detail ?? undefined,
      failure: undefined,
    };
  }
  const llm = item.attemptedLlm === true;
  return {
    ...item,
    statement,
    status: "awaiting_extractor",
    errorMessage: undefined,
    failure: {
      detail: statement.error_detail ?? "unknown error",
      llm,
      // Reopened server-side by a re-upload: the failure is from an earlier session/attempt.
      ...(statement.status === "awaiting_extractor" ? { previous: true } : {}),
    },
    extractorChoice: !llm && llmConfigured ? LLM_CHOICE : item.extractorChoice,
  };
}

export type Action =
  | { type: "files_queued"; items: ImportItem[] }
  | {
      type: "upload_succeeded";
      clientId: string;
      statement: Statement;
      proposal: ImportItem["proposal"];
      llmConfigured?: boolean;
    }
  | { type: "upload_failed"; clientId: string; message: string }
  | { type: "issuer_selected"; clientId: string; issuerId: number | null; extractorChoice: ExtractorChoice | null }
  | { type: "extractor_selected"; clientId: string; extractorChoice: ExtractorChoice }
  | { type: "pages_selected"; clientId: string; pages: number[] | null }
  | { type: "remember_toggled"; clientId: string; remember: boolean }
  | { type: "extract_started"; clientId: string; jobId: string }
  | { type: "extract_failed"; clientId: string; message: string }
  | { type: "job_event"; clientId: string; event: JobEvent }
  | { type: "statement_refreshed"; clientId: string; statement: Statement; llmConfigured?: boolean }
  | { type: "needs_review_counted"; clientId: string; count: number | undefined };

function reducer(items: ImportItem[], action: Action): ImportItem[] {
  const patch = (clientId: string, fn: (item: ImportItem) => ImportItem): ImportItem[] =>
    items.map((item) => (item.clientId === clientId ? fn(item) : item));

  switch (action.type) {
    case "files_queued":
      return [...items, ...action.items];

    case "upload_failed":
      return patch(action.clientId, (item) => ({
        ...item,
        status: "upload_failed",
        errorMessage: action.message,
      }));

    case "upload_succeeded":
      return patch(action.clientId, (item) =>
        applyStatement(
          {
            ...item,
            statementId: action.statement.id,
            proposal: action.proposal,
            selectedPages: action.statement.selected_pages ?? null,
            issuerId: action.proposal?.issuer_id ?? null,
            extractorChoice: action.proposal ? extractorChoiceFromProposal(action.proposal) : null,
          },
          action.statement,
          action.llmConfigured ?? false,
        ),
      );

    case "issuer_selected":
      return patch(action.clientId, (item) => ({
        ...item,
        issuerId: action.issuerId,
        extractorChoice: action.extractorChoice,
      }));

    case "extractor_selected":
      return patch(action.clientId, (item) => ({ ...item, extractorChoice: action.extractorChoice }));

    case "pages_selected":
      return patch(action.clientId, (item) => ({ ...item, selectedPages: action.pages }));

    case "remember_toggled":
      return patch(action.clientId, (item) => ({ ...item, remember: action.remember }));

    case "extract_started":
      return patch(action.clientId, (item) => ({
        ...item,
        status: "extracting",
        jobId: action.jobId,
        errorMessage: undefined,
        failure: undefined,
        attemptedLlm: isLlmChoice(item.extractorChoice),
      }));

    case "extract_failed":
      return patch(action.clientId, (item) => ({
        ...item,
        status: "awaiting_extractor",
        errorMessage: action.message,
      }));

    case "job_event":
      return patch(action.clientId, (item) => ({ ...item, jobEvent: action.event }));

    case "statement_refreshed":
      return patch(action.clientId, (item) =>
        applyStatement(item, action.statement, action.llmConfigured ?? false),
      );

    case "needs_review_counted":
      return patch(action.clientId, (item) => ({ ...item, needsReviewCount: action.count }));

    default:
      return items;
  }
}

export interface UseImportQueueResult {
  items: ImportItem[];
  /** Queue newly dropped/picked files for `userId`; returns their assigned `clientId`s so the
   * caller can kick off the upload for each. */
  queueFiles: (files: File[], userId: number) => string[];
  dispatch: React.Dispatch<Action>;
}

/**
 * Owns the Import screen's per-file state machine (`types.ts`'s `ImportItem`). Pure reducer plus
 * a thin `queueFiles` convenience — the actual network calls (upload, extract, job polling,
 * needs-review counting) are orchestrated by `index.tsx`, which dispatches the results here so
 * this hook stays a plain, easily-tested state container.
 */
export function useImportQueue(): UseImportQueueResult {
  const [items, dispatch] = React.useReducer(reducer, []);

  const queueFiles = React.useCallback((files: File[], userId: number): string[] => {
    const newItems: ImportItem[] = files.map((file) => ({
      clientId: newClientId(),
      fileName: file.name,
      userId,
      status: "uploading",
      issuerId: null,
      extractorChoice: null,
      remember: true,
    }));
    dispatch({ type: "files_queued", items: newItems });
    return newItems.map((item) => item.clientId);
  }, []);

  return { items, queueFiles, dispatch };
}

/** Whether the chosen extractor is the LLM statement parser. */
export function isLlmChoice(choice: ExtractorChoice | null): boolean {
  return choice?.kind === "parser" && choice.parserId === "llm";
}

/** Whether `item`'s current proposal is confident enough to import in one click (§2f.2). */
export function isConfidentProposal(item: ImportItem): boolean {
  return (
    !isLlmChoice(item.extractorChoice) &&
    item.issuerId !== null &&
    item.extractorChoice !== null &&
    (item.proposal?.confidence ?? 0) >= CONFIDENT_SCORE_THRESHOLD
  );
}

/** Whether `item` should skip the confirmation step entirely (a confident proposal from an
 * issuer the user has told the screen not to ask about again). */
export function shouldAutoConfirm(item: ImportItem): boolean {
  // Never auto-import with the LLM: it sends statement text off-device (or to a local model) and
  // its output always needs a human look first.
  // Nor re-run a built-in parser that already failed on this statement.
  return (
    !isLlmChoice(item.extractorChoice) &&
    item.failure === undefined &&
    isConfidentProposal(item) &&
    item.issuerId !== null &&
    isSkipConfirmForIssuer(item.issuerId)
  );
}

/** Refreshes a statement after its extract job reaches a terminal state, then — once parsed —
 * gets its exact needs-review count: first by waiting on the chained classify job's own count
 * (`pollClassifyJobNeedsReview`), falling back to `fetchNeedsReviewCount`'s `statement_id` query
 * if the import job never chained one. Takes only the ids it needs (not the full `ImportItem`)
 * so it never closes over a stale item from before the job finished.
 *
 * Also invalidates every other screen's cached queries (`invalidateQueries()` with no key
 * filter — screens each own their own query-key namespace, so there is no single shared key to
 * target). Without this, a screen visited earlier in the same session — the Spending overview, most
 * visibly — keeps showing the "before this import" snapshot it cached, for as long as TanStack
 * Query's 30 s `staleTime` (§4) has not yet elapsed, even though new transactions now exist.
 */
export async function refreshAfterJob(
  clientId: string,
  statementId: number,
  jobId: string,
  dispatch: React.Dispatch<Action>,
  queryClient: QueryClient,
  llmConfigured = false,
): Promise<void> {
  const statement = await fetchStatement(statementId);
  if (statement === null) return;
  dispatch({ type: "statement_refreshed", clientId, statement, llmConfigured });

  if (statement.status === "parsed") {
    const count =
      (await pollClassifyJobNeedsReview(jobId)) ?? (await fetchNeedsReviewCount({ statementId }));
    dispatch({ type: "needs_review_counted", clientId, count });
    void queryClient.invalidateQueries();
  }
}
