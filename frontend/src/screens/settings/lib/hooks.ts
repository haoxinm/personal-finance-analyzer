import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient, throwIfError, type components } from "@/api/client";

import { clearApiKey, setApiKey } from "./api-key";

export type Settings = components["schemas"]["Settings"];
export type LlmSettings = components["schemas"]["LlmSettings"];
export type PrivacySettings = components["schemas"]["PrivacySettings"];
export type IngestSettings = components["schemas"]["IngestSettings"];
export type ServerSettings = components["schemas"]["ServerSettings"];
export type TaxSettings = components["schemas"]["TaxSettings"];
export type TaxParameters = components["schemas"]["TaxParameters"];
export type TaxParametersRefresh = components["schemas"]["TaxParametersRefreshResponse"];
export type User = components["schemas"]["User"];
export type UserCreate = components["schemas"]["UserCreate"];
export type UserUpdate = components["schemas"]["UserUpdate"];
export type Account = components["schemas"]["Account"];
export type AccountCreate = components["schemas"]["AccountCreate"];
export type AccountUpdate = components["schemas"]["AccountUpdate"];
export type Issuer = components["schemas"]["Issuer"];
export type ClassifyPreviewRow = components["schemas"]["ClassifyPreviewRow"];
export type TestLlmResponse = components["schemas"]["TestLlmResponse"];

// ---------------------------------------------------------------------------
// Settings (§3.10, §3.12) — PUT rewrites the whole document, so callers merge
// their section's changes into the last-known Settings before saving.
// ---------------------------------------------------------------------------

export function useSettingsQuery() {
  return useQuery({
    queryKey: ["settings"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/settings");
      throwIfError(error);
      return data;
    },
  });
}

export function useUpdateSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: Settings) => {
      const { data, error } = await apiClient.PUT("/settings", { body });
      throwIfError(error);
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(["settings"], data);
      // LLM and privacy settings change exactly what a preview would show.
      void queryClient.invalidateQueries({ queryKey: ["llm-preview"] });
      // The tax parameter status reports the `auto_update_params` setting.
      void queryClient.invalidateQueries({ queryKey: TAX_PARAMETERS_KEY });
    },
  });
}

// ---------------------------------------------------------------------------
// Tax parameters — which IRS parameter file is in effect and the opt-in yearly update check.
// ---------------------------------------------------------------------------

const TAX_PARAMETERS_KEY = ["tax", "parameters"] as const;

export function useTaxParametersQuery() {
  return useQuery({
    queryKey: TAX_PARAMETERS_KEY,
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/tax/parameters");
      throwIfError(error);
      if (!data) throw new Error("The server returned no tax parameters.");
      return data;
    },
  });
}

/** "Check now": one update check on request (works with the opt-in off, because the user asked). */
export function useRefreshTaxParameters() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/tax/parameters/refresh");
      throwIfError(error);
      if (!data) throw new Error("The server returned no result.");
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(TAX_PARAMETERS_KEY, data.parameters);
      // A newer file changes every estimate and summary headline.
      void queryClient.invalidateQueries({ queryKey: ["tax"] });
    },
  });
}

export function useTestLlm() {
  return useMutation({
    // Tests the unsaved draft, not whatever is on disk.
    mutationFn: async (llm: LlmSettings) => {
      const { data, error } = await apiClient.POST("/settings/test-llm", { body: { llm } });
      throwIfError(error);
      return data;
    },
  });
}

export function useSetApiKey() {
  return useMutation({
    mutationFn: ({ baseUrl, apiKey }: { baseUrl: string; apiKey: string }) =>
      setApiKey(baseUrl, apiKey),
  });
}

export function useClearApiKey() {
  return useMutation({
    mutationFn: ({ baseUrl }: { baseUrl: string }) => clearApiKey(baseUrl),
  });
}

// ---------------------------------------------------------------------------
// Users (§3.12)
// ---------------------------------------------------------------------------

export function useUsersQuery() {
  return useQuery({
    queryKey: ["users"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/users");
      throwIfError(error);
      return data ?? [];
    },
  });
}

export function useCreateUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: UserCreate) => {
      const { data, error } = await apiClient.POST("/users", { body });
      throwIfError(error);
      return data;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["users"] }),
  });
}

export function useUpdateUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, body }: { id: number; body: UserUpdate }) => {
      const { data, error } = await apiClient.PATCH("/users/{user_id}", {
        params: { path: { user_id: id } },
        body,
      });
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["users"] });
      // A user's personal-info aliases are redacted in previews.
      void queryClient.invalidateQueries({ queryKey: ["llm-preview"] });
    },
  });
}

export function useDeleteUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => {
      const { error } = await apiClient.DELETE("/users/{user_id}", {
        params: { path: { user_id: id } },
      });
      throwIfError(error);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["users"] });
      void queryClient.invalidateQueries({ queryKey: ["accounts"] });
    },
  });
}

// ---------------------------------------------------------------------------
// Accounts (§3.12)
// ---------------------------------------------------------------------------

export function useAccountsQuery() {
  return useQuery({
    queryKey: ["accounts"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/accounts");
      throwIfError(error);
      return data ?? [];
    },
  });
}

export function useCreateAccount() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: AccountCreate) => {
      const { data, error } = await apiClient.POST("/accounts", { body });
      throwIfError(error);
      return data;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["accounts"] }),
  });
}

export function useUpdateAccount() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, body }: { id: number; body: AccountUpdate }) => {
      const { data, error } = await apiClient.PATCH("/accounts/{account_id}", {
        params: { path: { account_id: id } },
        body,
      });
      throwIfError(error);
      return data;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["accounts"] }),
  });
}

// ---------------------------------------------------------------------------
// Issuers — read-only here (issuer CRUD with match_terms is owned by P3-F);
// this screen only needs issuer names to label accounts.
// ---------------------------------------------------------------------------

export function useIssuersQuery() {
  return useQuery({
    queryKey: ["issuers"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/issuers");
      throwIfError(error);
      return data ?? [];
    },
  });
}

// ---------------------------------------------------------------------------
// Egress preview (§3.12, §3.7) — GET /classify/preview makes no network call;
// it decodes the exact CSV a run would send back into rows.
// ---------------------------------------------------------------------------

export function useClassifyPreview(params: { userId?: number; statementId?: number }) {
  return useQuery({
    queryKey: ["classify-preview", params],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/classify/preview", {
        params: { query: { user_id: params.userId, statement_id: params.statementId } },
      });
      throwIfError(error);
      return data ?? [];
    },
  });
}
