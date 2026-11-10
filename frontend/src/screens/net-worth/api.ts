import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient, throwIfError, type components } from "@/api/client";

export type BalanceEntry = components["schemas"]["BalanceEntry"];
export type BalanceEntryUpsert = components["schemas"]["BalanceEntryUpsert"];
export type NetWorthSummary = components["schemas"]["NetWorthSummary"];

const ROOT_KEY = ["net-worth"] as const;

/** `GET /net-worth/entries` for one user (or everyone when `userId` is null). */
export function useBalanceEntries(userId: number | null, enabled = true) {
  return useQuery({
    enabled,
    queryKey: [...ROOT_KEY, "entries", { userId }],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/net-worth/entries", {
        params: { query: { user_id: userId ?? undefined } },
      });
      throwIfError(error);
      return Array.isArray(data) ? data : [];
    },
  });
}

export interface SummaryArgs {
  userId: number | null;
  currency: string;
  /** Snapshot period "YYYY-MM"; omitted = latest. */
  period?: string;
  enabled?: boolean;
}

/** `GET /net-worth/summary`; `user_id` omitted = all users combined. */
export function useNetWorthSummary({ userId, currency, period, enabled = true }: SummaryArgs) {
  return useQuery({
    enabled,
    queryKey: [...ROOT_KEY, "summary", { userId, currency, period }],
    // Keep the previous snapshot on screen while another period loads (same user only, so one
    // user's numbers never flash under another's name).
    placeholderData: (previous, previousQuery) =>
      (previousQuery?.queryKey[2] as { userId: number | null } | undefined)?.userId === userId
        ? previous
        : undefined,
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/net-worth/summary", {
        params: { query: { user_id: userId ?? undefined, currency, period } },
      });
      throwIfError(error);
      // Guard against a non-summary payload (e.g. an empty list from a stub or proxy).
      return data && !Array.isArray(data) ? data : null;
    },
  });
}

export interface UpsertResult {
  entry: BalanceEntry;
  /** True for 201 (new row), false for 200 (an existing period was overwritten). */
  created: boolean;
}

export function useUpsertBalanceEntry() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: BalanceEntryUpsert): Promise<UpsertResult> => {
      const { data, error, response } = await apiClient.POST("/net-worth/entries", { body });
      throwIfError(error);
      return { entry: data as BalanceEntry, created: response.status === 201 };
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ROOT_KEY });
    },
  });
}

export function usePatchBalanceEntry() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (args: { id: number; balance_minor?: number; note?: string | null }) => {
      const { id, ...body } = args;
      const { data, error } = await apiClient.PATCH("/net-worth/entries/{id}", {
        params: { path: { id } },
        body,
      });
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ROOT_KEY });
    },
  });
}

export function useDeleteBalanceEntry() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => {
      const { error } = await apiClient.DELETE("/net-worth/entries/{id}", {
        params: { path: { id } },
      });
      throwIfError(error);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ROOT_KEY });
    },
  });
}
