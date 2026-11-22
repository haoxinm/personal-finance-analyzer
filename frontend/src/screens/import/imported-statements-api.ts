import { useMutation, useQueryClient } from "@tanstack/react-query";

import { apiClient, throwIfError, type components } from "@/api/client";

/** Mutations of the "Imported statements" panel. Query hooks (`useStatements`, `useAccounts`,
 * `useIssuers`, `useLayoutSpecs`) are reused from their owners. */

export type ReparseRequest = components["schemas"]["ReparseRequest"];

/** `POST /statements/{id}/reparse`: rebuild a statement from its stored PDF. Resolves with the job
 * id; a 400/409 (no extractor, never imported, no PDF kept) rejects with the server's sentence. */
export function useReparseStatement() {
  return useMutation({
    mutationFn: async ({ statementId, body }: { statementId: number; body: ReparseRequest }) => {
      const { data, error } = await apiClient.POST("/statements/{statement_id}/reparse", {
        params: { path: { statement_id: statementId } },
        body,
      });
      throwIfError(error);
      if (!data) throw new Error("re-import returned no job");
      return data.job_id;
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
