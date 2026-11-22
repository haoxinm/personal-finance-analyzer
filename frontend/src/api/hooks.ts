import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient, throwIfError, type components } from "./client";

/**
 * A small set of typed query/mutation hooks built on `apiClient`, enough to prove the
 * generated client works end to end and to give Phase 3 a pattern to copy. Phase 3 screens
 * add whatever further hooks they need directly against `apiClient` — this file is not meant
 * to grow into a hook per endpoint.
 */

export function useUsers() {
  return useQuery({
    queryKey: ["users"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/users");
      throwIfError(error);
      return data;
    },
  });
}

export function useCreateUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["UserCreate"]) => {
      const { data, error } = await apiClient.POST("/users", { body });
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["users"] });
    },
  });
}

export function useStatements(userId?: number) {
  return useQuery({
    queryKey: ["statements", { userId }],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/statements", {
        params: { query: { user_id: userId } },
      });
      throwIfError(error);
      return data;
    },
  });
}

export function useTransactions(query: {
  user_ids?: number[];
  account_ids?: number[];
  date_from?: string;
  date_to?: string;
  category_keys?: string[];
  search?: string;
  page?: number;
  page_size?: number;
}) {
  return useQuery({
    queryKey: ["transactions", query],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/transactions", {
        params: { query },
      });
      throwIfError(error);
      return data;
    },
  });
}

export function usePatchTransaction() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      body,
    }: {
      id: number;
      body: components["schemas"]["TransactionPatch"];
    }) => {
      const { data, error } = await apiClient.PATCH("/transactions/{id}", {
        params: { path: { id } },
        body,
      });
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      // A corrected date or amount moves charts and totals, and the review queue's counts.
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
      void queryClient.invalidateQueries({ queryKey: ["analytics"] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      void queryClient.invalidateQueries({ queryKey: ["review"] });
      void queryClient.invalidateQueries({ queryKey: ["statements"] });
    },
  });
}

export function useAnalyticsSummary(query: {
  user_ids?: number[];
  account_ids?: number[];
  date_from?: string;
  date_to?: string;
  granularity?: "day" | "week" | "month" | "quarter" | "year" | "all";
  group_by?: Array<"category" | "subcategory" | "user" | "account" | "merchant" | "period">;
  category_keys?: string[];
  kinds?: Array<components["schemas"]["Kind"]>;
  include_non_spend?: boolean;
  net_refunds?: boolean;
  currency?: string;
}) {
  return useQuery({
    queryKey: ["analytics", "summary", query],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/analytics/summary", {
        params: { query },
      });
      throwIfError(error);
      return data;
    },
  });
}

export function useTaxonomy() {
  return useQuery({
    queryKey: ["taxonomy"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/taxonomy");
      throwIfError(error);
      return data;
    },
  });
}

export function useSettings() {
  return useQuery({
    queryKey: ["settings"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/settings");
      throwIfError(error);
      return data;
    },
  });
}
