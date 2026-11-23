import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { apiClient, throwIfError, type components } from "@/api/client";

import { fetchJob } from "./api";

/** Mutations of the "Imported statements" panel. Query hooks (`useStatements`, `useAccounts`,
 * `useIssuers`, `useLayoutSpecs`) are reused from their owners. */

export type ReparseRequest = components["schemas"]["ReparseRequest"];
export type ExtractorCandidate = components["schemas"]["ExtractorCandidate"];
export type StatementPatchResult = components["schemas"]["StatementPatchResult"];

const TERMINAL = new Set(["done", "error", "cancelled"]);
/** Polling starts quick and backs off, so a slow LLM job is followed for a long while without
 * hammering the server; each job is followed for at most `POLL_BUDGET_MS`. */
const POLL_START_MS = 1000;
const POLL_CAP_MS = 15_000;
const POLL_FACTOR = 1.5;
const POLL_BUDGET_MS = 30 * 60 * 1000;

/** Jobs already being followed (keyed per query client), so a job is never followed twice. */
const following = new WeakMap<QueryClient, Set<string>>();

/** Follow a job (and the classification job it spawned) to the end, refreshing everything that
 * shows imported data after each, whatever happens to the component that started it. Accepts the
 * job id or the promise of one (a request still in flight); never throws. Module-level on purpose:
 * unmounting a panel must not lose the follow-up. A job id already being followed is skipped. */
export async function followInBackground(
  queryClient: QueryClient,
  jobIdOrPromise: string | Promise<string | null | undefined>,
): Promise<void> {
  let active = following.get(queryClient);
  if (!active) {
    active = new Set();
    following.set(queryClient, active);
  }
  const claimed: string[] = [];
  try {
    const first = await jobIdOrPromise;
    const seen = new Set<string>();
    let next: string | null | undefined = first;
    while (next && !seen.has(next)) {
      seen.add(next);
      if (active.has(next)) return; // another follower already has this job (and its classify job)
      active.add(next);
      claimed.push(next);
      let job: Awaited<ReturnType<typeof fetchJob>> = null;
      let delay = POLL_START_MS;
      let waited = 0;
      for (;;) {
        try {
          job = await fetchJob(next);
        } catch {
          job = null;
        }
        if (job && TERMINAL.has(job.status)) break;
        if (waited >= POLL_BUDGET_MS) break;
        await new Promise((resolve) => setTimeout(resolve, delay));
        waited += delay;
        delay = Math.min(POLL_CAP_MS, Math.round(delay * POLL_FACTOR));
      }
      void queryClient.invalidateQueries();
      next = job?.classify_job_id;
    }
  } catch {
    // The request itself failed: the caller shows that; there is nothing to follow.
  } finally {
    for (const id of claimed) active.delete(id);
  }
}

/** `POST /statements/{id}/reparse`: rebuild a statement from its stored PDF. Resolves with the job
 * id; a 400/409 (no extractor, never imported, no PDF kept, PDF gone) rejects with the server's
 * sentence. The job is followed in the background from the moment its id is known, so leaving the
 * panel (even mid-request) never loses the refresh of what the re-import changed. */
export function useReparseStatement() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ statementId, body }: { statementId: number; body: ReparseRequest }) => {
      const request = (async () => {
        const { data, error } = await apiClient.POST("/statements/{statement_id}/reparse", {
          params: { path: { statement_id: statementId } },
          body,
        });
        throwIfError(error);
        if (!data) throw new Error("re-import returned no job");
        return data.job_id;
      })();
      void followInBackground(queryClient, request);
      return request;
    },
  });
}

/** `GET /statements/{id}/extractors`: every extractor that could read this statement, scored on
 * its saved pages (the recorded and remembered ones marked). Needs the PDF, so it can 404/409. */
export function useExtractorCandidates(statementId: number, enabled = true) {
  return useQuery({
    queryKey: ["statement-extractors", statementId],
    enabled,
    retry: false,
    staleTime: 0,
    gcTime: 0,
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/statements/{statement_id}/extractors", {
        params: { path: { statement_id: statementId } },
      });
      throwIfError(error);
      return data ?? [];
    },
  });
}

/** `PATCH /statements/{id}` with a person: moves the statement and its transactions to that
 * person's matching account (created when missing). */
export function usePatchStatementOwner() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ statementId, userId }: { statementId: number; userId: number }) => {
      const { data, error } = await apiClient.PATCH("/statements/{statement_id}", {
        params: { path: { statement_id: statementId } },
        body: { user_id: userId },
      });
      throwIfError(error);
      if (!data) throw new Error("the change returned no result");
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries();
    },
  });
}

/** `DELETE /statements/{id}`: the statement, its transactions and the app's PDF copy; `deletePdf`
 * also removes a PDF that lives in the documents folder. */
export function useDeleteStatement() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ statementId, deletePdf }: { statementId: number; deletePdf: boolean }) => {
      const { error } = await apiClient.DELETE("/statements/{statement_id}", {
        params: { path: { statement_id: statementId }, query: { delete_pdf: deletePdf } },
      });
      throwIfError(error);
    },
    onSuccess: () => {
      // Everything that shows imported data (charts, grid, review queue, watch records) is stale.
      void queryClient.invalidateQueries();
    },
  });
}

export type StatementPatch = components["schemas"]["StatementPatch"];

/** `PATCH /statements/{id}` with period / bank / account changes. Resolves with the result
 * (`rows_moved`, `duplicates_deleted`); 400/404/409 reject with the server's sentence. */
export function usePatchStatementDetails() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ statementId, body }: { statementId: number; body: StatementPatch }) => {
      const { data, error } = await apiClient.PATCH("/statements/{statement_id}", {
        params: { path: { statement_id: statementId } },
        body,
      });
      throwIfError(error);
      if (!data) throw new Error("the change returned no result");
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries();
    },
  });
}

/** `PATCH /accounts/{id}` with a currency: relabels the account and every transaction on it. */
export function usePatchAccountCurrency() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ accountId, currency }: { accountId: number; currency: string }) => {
      const { data, error } = await apiClient.PATCH("/accounts/{account_id}", {
        params: { path: { account_id: accountId } },
        body: { currency },
      });
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries();
    },
  });
}
