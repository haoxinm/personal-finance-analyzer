import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { apiClient, throwIfError, type components } from "@/api/client";

import { fetchJob } from "./api";

/** Mutations of the "Imported statements" panel. Query hooks (`useStatements`, `useAccounts`,
 * `useIssuers`, `useLayoutSpecs`) are reused from their owners. */

export type ReparseRequest = components["schemas"]["ReparseRequest"];
export type ExtractorCandidate = components["schemas"]["ExtractorCandidate"];
export type StatementPatchResult = components["schemas"]["StatementPatchResult"];

const TERMINAL = new Set(["done", "error", "cancelled"]);
const POLL_MS = 1000;
const POLL_MAX = 180;

/** Follow a job (and the classification job it spawned) to the end, refreshing everything that
 * shows imported data after each, whatever happens to the component that started it. Accepts the
 * job id or the promise of one (a request still in flight); never throws. Module-level on purpose:
 * unmounting a panel must not lose the follow-up. */
export async function followInBackground(
  queryClient: QueryClient,
  jobIdOrPromise: string | Promise<string | null | undefined>,
): Promise<void> {
  try {
    const first = await jobIdOrPromise;
    const seen = new Set<string>();
    let next: string | null | undefined = first;
    while (next && !seen.has(next)) {
      seen.add(next);
      let job: Awaited<ReturnType<typeof fetchJob>> = null;
      for (let attempt = 0; attempt < POLL_MAX; attempt += 1) {
        try {
          job = await fetchJob(next);
        } catch {
          job = null;
        }
        if (job && TERMINAL.has(job.status)) break;
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
      }
      void queryClient.invalidateQueries();
      next = job?.classify_job_id;
    }
  } catch {
    // The request itself failed: the caller shows that; there is nothing to follow.
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
