import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient, throwIfError, type components } from "@/api/client";

/**
 * Screen-local query/mutation hooks for the Rules & Taxonomy screen (P3-F): rule CRUD, issuer
 * CRUD, layout-spec listing/approve/revise/paste, and the taxonomy approve/merge actions. Kept
 * inside `screens/rules/**` per the Phase 3 contract — no shared hook file grows for this.
 */

// --- Rules -------------------------------------------------------------------------------------

export function useRules() {
  return useQuery({
    queryKey: ["rules"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/rules");
      throwIfError(error);
      return data ?? [];
    },
  });
}

export function useCreateRule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["RuleCreate"]) => {
      const { data, error } = await apiClient.POST("/rules", { body });
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["rules"] });
    },
  });
}

export type RuleApplyPreview = components["schemas"]["RuleApplyPreview"];
export type RuleApplyResult = {
  updated: number;
  /** Hand to `useUndoApply` to put the changed rows back. */
  undo: components["schemas"]["AcceptUndo"];
};

/**
 * `GET /rules/{id}/apply-preview`: what applying the rule to the transactions already imported
 * would change right now (row count, owning users, kind changes) — nothing is changed. A
 * mutation, not a query: it is asked once per click, and the answer must not be a stale cache.
 */
export function useRuleApplyPreview() {
  return useMutation({
    mutationFn: async (id: number): Promise<RuleApplyPreview> => {
      const { data, error } = await apiClient.GET("/rules/{rule_id}/apply-preview", {
        params: { path: { rule_id: id } },
      });
      throwIfError(error);
      if (!data) throw new Error("apply preview returned no body");
      return data;
    },
  });
}

/**
 * `POST /rules/{id}/apply`: apply a rule to the transactions already imported (a new rule otherwise
 * only affects future classification). Skips anything the user categorized by hand and anything a
 * higher-priority rule claims. Resolves to how many rows changed and an undo token.
 */
export function useApplyRule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: number): Promise<RuleApplyResult> => {
      const { data, error } = await apiClient.POST("/rules/{rule_id}/apply", {
        params: { path: { rule_id: id } },
      });
      throwIfError(error);
      if (!data) throw new Error("apply returned no body");
      return { updated: data.updated, undo: data.undo };
    },
    onSuccess: () => {
      // Every screen's cached transactions, review queue and analytics are now stale.
      void queryClient.invalidateQueries();
    },
  });
}

/**
 * `POST /review/undo` with the token `useApplyRule` returned: puts the changed rows back, except
 * any edited since (the server skips and reports those).
 */
export function useUndoApply() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (undo: RuleApplyResult["undo"]) => {
      const { data, error } = await apiClient.POST("/review/undo", { body: undo });
      throwIfError(error);
      return { restored: data?.restored ?? 0, skipped: data?.skipped ?? [] };
    },
    onSuccess: () => {
      void queryClient.invalidateQueries();
    },
  });
}

export function useUpdateRule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      body,
    }: {
      id: number;
      body: components["schemas"]["RuleUpdate"];
    }) => {
      const { data, error } = await apiClient.PATCH("/rules/{rule_id}", {
        params: { path: { rule_id: id } },
        body,
      });
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["rules"] });
    },
  });
}

export function useDeleteRule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => {
      const { error } = await apiClient.DELETE("/rules/{rule_id}", {
        params: { path: { rule_id: id } },
      });
      throwIfError(error);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["rules"] });
    },
  });
}

// --- Transactions (read-only, for the rule match preview) --------------------------------------

/** The largest single page the backend will return (`crud.list_transactions` caps at 500). */
export const MAX_TRANSACTION_PAGE_SIZE = 500;

/**
 * Fetches up to `maxPages * MAX_TRANSACTION_PAGE_SIZE` transactions (newest first) for the rule
 * match preview to filter client-side against `merchant_key` (never exposed as a server-side
 * filter — see `rule-matching.ts`'s header). `include_non_spend` is always true so the preview
 * covers every kind a rule could apply to, not just the analytics-default subset.
 */
export function useTransactionsForPreview(enabled: boolean, maxPages = 4) {
  return useQuery({
    queryKey: ["rules", "transactions-preview-pool", maxPages],
    enabled,
    staleTime: 60_000,
    queryFn: async () => {
      const first = await apiClient.GET("/transactions", {
        params: {
          query: { include_non_spend: true, page: 1, page_size: MAX_TRANSACTION_PAGE_SIZE, sort: "-posted_date" },
        },
      });
      throwIfError(first.error);
      const items = [...(first.data?.items ?? [])];
      const total = first.data?.total ?? items.length;

      // Fetch only as many further pages as `total` actually needs, capped at `maxPages` —
      // avoids re-fetching pages the server has nothing left to return for.
      const pagesNeeded = Math.min(maxPages, Math.ceil(total / MAX_TRANSACTION_PAGE_SIZE));
      if (pagesNeeded > 1 && items.length >= MAX_TRANSACTION_PAGE_SIZE) {
        const rest = await Promise.all(
          Array.from({ length: pagesNeeded - 1 }, (_, i) =>
            apiClient.GET("/transactions", {
              params: {
                query: {
                  include_non_spend: true,
                  page: i + 2,
                  page_size: MAX_TRANSACTION_PAGE_SIZE,
                  sort: "-posted_date",
                },
              },
            }),
          ),
        );
        for (const { error } of rest) throwIfError(error);
        for (const page of rest) items.push(...(page.data?.items ?? []));
      }
      return { items, total, truncated: total > items.length };
    },
  });
}

// --- Issuers -------------------------------------------------------------------------------------

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
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["issuers"] });
    },
  });
}

export function useUpdateIssuer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      body,
    }: {
      id: number;
      body: components["schemas"]["IssuerUpdate"];
    }) => {
      const { data, error } = await apiClient.PATCH("/issuers/{issuer_id}", {
        params: { path: { issuer_id: id } },
        body,
      });
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["issuers"] });
    },
  });
}

export function useDeleteIssuer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => {
      const { error } = await apiClient.DELETE("/issuers/{issuer_id}", {
        params: { path: { issuer_id: id } },
      });
      throwIfError(error);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["issuers"] });
    },
  });
}

/**
 * `POST /issuers/preview-match` (§3.12): the past statements a set of `match_terms` would match,
 * live, without saving anything — the "this would match these N past statements" check (§P3-F).
 */
export function usePreviewIssuerMatch(matchTerms: string[]) {
  return useQuery({
    queryKey: ["issuers", "preview-match", matchTerms],
    enabled: matchTerms.length > 0,
    queryFn: async () => {
      const { data, error } = await apiClient.POST("/issuers/preview-match", {
        body: { match_terms: matchTerms },
      });
      throwIfError(error);
      return data ?? [];
    },
  });
}

// --- Statements (read-only, for spec/parser usage counts and the issuer match check) -----------

export function useAllStatements() {
  return useQuery({
    queryKey: ["statements", "all"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/statements", { params: { query: {} } });
      throwIfError(error);
      return data ?? [];
    },
  });
}

// --- Layout specs --------------------------------------------------------------------------------

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

export function useCreateLayoutSpec() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["LayoutSpecCreate"]) => {
      const { data, error } = await apiClient.POST("/layout-specs", { body });
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["layout-specs"] });
    },
  });
}

export function useReviseLayoutSpec() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      body,
    }: {
      id: number;
      body: components["schemas"]["LayoutSpecCreate"];
    }) => {
      const { data, error } = await apiClient.POST("/layout-specs/{spec_id}/revise", {
        params: { path: { spec_id: id } },
        body,
      });
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["layout-specs"] });
    },
  });
}

export function useApproveLayoutSpec() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => {
      const { data, error } = await apiClient.POST("/layout-specs/{spec_id}/approve", {
        params: { path: { spec_id: id } },
      });
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["layout-specs"] });
    },
  });
}

/** Downloads a spec's YAML as a `<name>-v<version>.yaml` file via a `Blob` URL. */
export async function exportLayoutSpec(id: number, filename: string): Promise<void> {
  const { data, error } = await apiClient.GET("/layout-specs/{spec_id}/export", {
    params: { path: { spec_id: id } },
    parseAs: "text",
  });
  throwIfError(error);
  const blob = new Blob([String(data ?? "")], { type: "application/x-yaml" });
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
  } finally {
    URL.revokeObjectURL(url);
  }
}

// --- Taxonomy ------------------------------------------------------------------------------------

export function useTaxonomy() {
  return useQuery({
    queryKey: ["taxonomy"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/taxonomy");
      throwIfError(error);
      return data ?? [];
    },
  });
}

/** `POST /taxonomy/subcategories/{subcategory_id}/approve` (§3.12), keyed by `Subcategory.id`. */
export function useApproveSubcategory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (subcategoryId: number) => {
      const { data, error } = await apiClient.POST(
        "/taxonomy/subcategories/{subcategory_id}/approve",
        { params: { path: { subcategory_id: subcategoryId } } },
      );
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["taxonomy"] });
    },
  });
}

export function useMergeSubcategory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      subcategoryId,
      body,
    }: {
      subcategoryId: number;
      body: components["schemas"]["MergeRequest"];
    }) => {
      const { data, error } = await apiClient.POST(
        "/taxonomy/subcategories/{subcategory_id}/merge",
        { params: { path: { subcategory_id: subcategoryId } }, body },
      );
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["taxonomy"] });
      void queryClient.invalidateQueries({ queryKey: ["rules"] });
    },
  });
}
