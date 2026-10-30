import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient, throwIfError, type components } from "@/api/client";

/**
 * Import-screen-local API hooks (P3_BRIEF: "Put screen-specific query hooks inside your screen
 * dir"). `useUsers` already exists in `api/hooks.ts` and is reused as-is; everything here is
 * specific to the two-phase import flow (§2f.2) and has no home elsewhere yet.
 */

export function useIssuers() {
  return useQuery({
    queryKey: ["issuers"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/issuers");
      throwIfError(error);
      return data ?? [];
    },
  });
}

export function useCreateIssuer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["IssuerCreate"]) => {
      const { data, error } = await apiClient.POST("/issuers", { body });
      throwIfError(error);
      if (!data) throw new Error("issuer creation returned no data");
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["issuers"] });
    },
  });
}

export function useLayoutSpecs() {
  return useQuery({
    queryKey: ["layout-specs"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/layout-specs");
      throwIfError(error);
      return data ?? [];
    },
  });
}

export function useUploadStatement() {
  return useMutation({
    mutationFn: async ({ file, userId }: { file: File; userId: number }) => {
      const form = new FormData();
      form.append("file", file);
      form.append("user_id", String(userId));
      const { data, error } = await apiClient.POST("/statements", {
        // openapi-fetch's default body serializer passes a `FormData` body through unchanged
        // (letting the browser set the multipart boundary) instead of `JSON.stringify`-ing it;
        // the generated `Body_upload_statement_api_statements_post` type describes the decoded
        // *fields*, not the wire body, so a real `FormData` is cast to it here.
        body: form as unknown as components["schemas"]["Body_upload_statement_api_statements_post"],
      });
      throwIfError(error);
      if (!data) throw new Error("upload returned no data");
      return data;
    },
  });
}

export function useExtractStatement() {
  return useMutation({
    mutationFn: async ({
      statementId,
      body,
    }: {
      statementId: number;
      body: components["schemas"]["ExtractRequest"];
    }) => {
      const { data, error } = await apiClient.POST("/statements/{statement_id}/extract", {
        params: { path: { statement_id: statementId } },
        body,
      });
      throwIfError(error);
      if (!data) throw new Error("extract returned no data");
      return data;
    },
  });
}

export async function fetchStatement(
  statementId: number,
): Promise<components["schemas"]["Statement"] | null> {
  const { data, error } = await apiClient.GET("/statements/{statement_id}", {
    params: { path: { statement_id: statementId } },
  });
  if (error) return null;
  return data ?? null;
}

export async function fetchJob(jobId: string): Promise<components["schemas"]["Job"] | null> {
  const { data, error } = await apiClient.GET("/jobs/{job_id}", {
    params: { path: { job_id: jobId } },
  });
  if (error) return null;
  return data ?? null;
}

/**
 * Exact count of this statement's transactions still flagged `needs_review` (§3.12's
 * `Transaction.needs_review`), for the summary line's "N need review" clause, via
 * `GET /transactions?statement_id=&needs_review=true` (`page_size: 1`, reading `total`).
 */
export async function fetchNeedsReviewCount(params: {
  statementId: number;
}): Promise<number | undefined> {
  const { data, error } = await apiClient.GET("/transactions", {
    params: {
      query: {
        statement_id: params.statementId,
        needs_review: true,
        page: 1,
        page_size: 1,
        include_non_spend: true,
      },
    },
  });
  if (error || !data) return undefined;
  return data.total;
}

/**
 * Polls the classify job chained after an import job (`Job.classify_job_id`, §3.12a) until it
 * reaches a terminal state, then returns its own `needs_review` count (§3.12 `/jobs/{id}`) —
 * exact, and cheaper than scanning `/transactions` once the cascade has already computed it.
 * Returns `undefined` if the import job never chained a classify job, or a job fetch fails, so
 * the caller can fall back to `fetchNeedsReviewCount`.
 */
export async function pollClassifyJobNeedsReview(
  importJobId: string,
  options: {
    intervalMs?: number;
    maxAttempts?: number;
    /** The already-fetched import job, to save re-fetching it. */
    importJob?: components["schemas"]["Job"] | null;
  } = {},
): Promise<number | undefined> {
  const importJob =
    options.importJob !== undefined ? options.importJob : await fetchJob(importJobId);
  const classifyJobId = importJob?.classify_job_id;
  if (!classifyJobId) return undefined;

  const intervalMs = options.intervalMs ?? 200;
  const maxAttempts = options.maxAttempts ?? 50; // ~10s ceiling

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const job = await fetchJob(classifyJobId);
    if (!job) return undefined;
    if (job.status === "done" || job.status === "error" || job.status === "cancelled") {
      return job.needs_review ?? undefined;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return undefined;
}

export function useImportSettings() {
  return useQuery({
    queryKey: ["settings"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/settings");
      throwIfError(error);
      return data;
    },
  });
}

export function useStatementPages(statementId: number | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["statement-pages", statementId],
    enabled: enabled && statementId !== undefined,
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/statements/{statement_id}/pages", {
        params: { path: { statement_id: statementId as number } },
      });
      throwIfError(error);
      return data ?? [];
    },
  });
}

/** Exactly what the LLM parser would send for `pages` (null = all). Called only on demand. */
export async function fetchStatementLlmPreview(statementId: number, pages: number[] | null) {
  const { data, error } = await apiClient.GET("/statements/{statement_id}/llm-preview", {
    params: {
      path: { statement_id: statementId },
      query: { pages: pages === null ? undefined : pages.join(",") },
    },
  });
  throwIfError(error);
  if (!data) throw new Error("preview returned no data");
  return data;
}
