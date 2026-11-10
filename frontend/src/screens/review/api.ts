import { apiClient, throwIfError, type components } from "@/api/client";

import type { AcceptUndoToken, Transaction } from "./types";

export type TransactionPatchBody = components["schemas"]["TransactionPatch"];
export type ReviewAcceptResponse = components["schemas"]["ReviewAcceptResponse"];

/**
 * The largest page the backend will hand back in one request (`crud.list_transactions` clamps
 * `page_size` to 500, §3.12 `/transactions`).
 */
const PAGE_SIZE = 500;

/**
 * A hard ceiling on how many pages this screen will fetch when building the review queue.
 * 50 pages * 500 rows = 25,000 transactions, comfortably past what a single-user local install
 * accumulates before this stops being "the whole queue".
 */
const MAX_PAGES = 50;

/**
 * Fetches every `needs_review` transaction, oldest first, via `GET /transactions?needs_review=true`
 * (with `include_non_spend: true` so a low-confidence payment or transfer is never silently
 * skipped), optionally restricted to `userIds` (the per-user view), paging server-side until a short page signals the last one.
 */
export async function fetchNeedsReviewQueue(userIds?: number[]): Promise<Transaction[]> {
  const collected: Transaction[] = [];
  for (let page = 1; page <= MAX_PAGES; page += 1) {
    const { data, error } = await apiClient.GET("/transactions", {
      params: {
        query: {
          page,
          page_size: PAGE_SIZE,
          sort: "posted_date",
          include_non_spend: true,
          needs_review: true,
          user_ids: userIds,
          // Each row's per-layer suggestions, for the card's one-click chips.
          include_candidates: true,
        },
      },
    });
    throwIfError(error);
    if (!data) break;
    collected.push(...data.items);
    if (data.items.length < PAGE_SIZE) break; // last page
  }
  return collected;
}

/**
 * `POST /review/accept`: accept one queue row as `categoryKey/subcategoryKey` — a one-time edit
 * of that transaction only (it clears `needs_review`, is marked as the user's own, and writes no
 * rule or merchant memory, I6) — optionally also as the merchant's standing rule
 * (`createRule`, "always categorize this merchant this way"), and for `similarIds` — other
 * pending rows of the same merchant — as further one-time edits. The response's `undo` token goes
 * back to `undoReview` to reverse all of it.
 */
export async function acceptReview(body: {
  transactionId: number;
  categoryKey: string;
  subcategoryKey: string;
  createRule: boolean;
  similarIds: number[];
  /** A suggestion from a rule with a kind override also sets this row's kind. */
  kind?: Transaction["kind"];
}): Promise<ReviewAcceptResponse> {
  const { data, error } = await apiClient.POST("/review/accept", {
    body: {
      transaction_id: body.transactionId,
      category_key: body.categoryKey,
      subcategory_key: body.subcategoryKey,
      create_rule: body.createRule,
      similar_ids: body.similarIds,
      ...(body.kind ? { kind: body.kind } : {}),
    },
  });
  throwIfError(error);
  if (!data) throw new Error("accept returned no body");
  return data;
}

export interface UndoOutcome {
  /** Transactions put back. */
  restored: number;
  /** Transaction ids left alone because they were edited (or deleted) after the accept. */
  skipped: number[];
  /** The accept's rule was left alone because it was edited or deleted since. */
  ruleSkipped: boolean;
}

/**
 * `POST /review/undo`: put back everything one accept (or one "apply rule to existing") changed,
 * except what was edited since: the server skips and reports those rather than overwrite them.
 */
export async function undoReview(undo: AcceptUndoToken): Promise<UndoOutcome> {
  const { data, error } = await apiClient.POST("/review/undo", { body: undo });
  throwIfError(error);
  return {
    restored: data?.restored ?? 0,
    skipped: data?.skipped ?? [],
    ruleSkipped: data?.rule_skipped ?? false,
  };
}

/**
 * `POST /classify/run`: re-run the cascade over every transaction that is uncategorized, parked
 * in `others/uncategorized`, or still flagged for review (never one the user categorized
 * themselves). Resolves to the background job to watch.
 */
export async function startReclassify(
  userId: number | null,
): Promise<{ jobId: string; transactionCount: number }> {
  const { data, error } = await apiClient.POST("/classify/run", {
    body: { user_id: userId, only_unclassified: true },
  });
  throwIfError(error);
  if (!data) throw new Error("classify run returned no body");
  return { jobId: data.job_id, transactionCount: data.transaction_count };
}

export async function fetchJobStatus(jobId: string) {
  const { data, error } = await apiClient.GET("/jobs/{job_id}", {
    params: { path: { job_id: jobId } },
  });
  throwIfError(error);
  if (!data) throw new Error("job lookup returned no body");
  return data;
}

/** How many transactions are uncategorized (no category, or `others/uncategorized`), via
 * `GET /transactions?uncategorized=true` read as `total`. Payments/transfers are included: they
 * are never uncategorized (the cascade files them) so that does not inflate the count. */
export async function fetchUncategorizedCount(userIds?: number[]): Promise<number> {
  const { data, error } = await apiClient.GET("/transactions", {
    params: {
      query: {
        uncategorized: true,
        include_non_spend: true,
        page: 1,
        page_size: 1,
        user_ids: userIds,
      },
    },
  });
  throwIfError(error);
  return data?.total ?? 0;
}
