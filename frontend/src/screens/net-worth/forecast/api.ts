import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient, throwIfError } from "@/api/client";

import type { ApiParams, SavedPreset } from "./params";

// Under the net-worth root key so adding a balance or a manual year refreshes the baseline.
const ROOT_KEY = ["net-worth"] as const;
const PRESETS_KEY = [...ROOT_KEY, "forecast", "presets"] as const;

/** `GET /net-worth/forecast/baseline`; `user_id` omitted = all users combined. */
export function useForecastBaseline(userId: number | null) {
  return useQuery({
    queryKey: [...ROOT_KEY, "forecast", "baseline", { userId }],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/net-worth/forecast/baseline", {
        params: { query: { user_id: userId ?? undefined } },
      });
      throwIfError(error);
      return data && !Array.isArray(data) ? data : null;
    },
  });
}

/** Manual year-end net worth: `PUT` (add or replace) and `DELETE` for one user and year. */
export function usePutHistory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (args: { userId: number; year: number; amountMinor: number; note: string | null }) => {
      const { data, error } = await apiClient.PUT("/net-worth/history/{user_id}/{year}", {
        params: { path: { user_id: args.userId, year: args.year } },
        body: { amount_minor: args.amountMinor, note: args.note },
      });
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ROOT_KEY });
    },
  });
}

export function useDeleteHistory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (args: { userId: number; year: number }) => {
      const { error } = await apiClient.DELETE("/net-worth/history/{user_id}/{year}", {
        params: { path: { user_id: args.userId, year: args.year } },
      });
      throwIfError(error);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ROOT_KEY });
    },
  });
}

/** Built-in presets plus the saved ones visible to this user (shared ones only for all users). */
export function useForecastPresets(userId: number | null) {
  return useQuery({
    queryKey: [...PRESETS_KEY, { userId }],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/net-worth/forecast/presets", {
        params: { query: { user_id: userId ?? undefined } },
      });
      throwIfError(error);
      return data && !Array.isArray(data) ? data : { builtin: [], saved: [] };
    },
  });
}

export function useCreatePreset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { name: string; userId: number | null; params: ApiParams }) => {
      const { data, error } = await apiClient.POST("/net-worth/forecast/presets", {
        body: { name: body.name, user_id: body.userId, params: body.params },
      });
      throwIfError(error);
      return data as SavedPreset;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: PRESETS_KEY });
    },
  });
}

export function useUpdatePreset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (args: { id: number; name?: string; params?: ApiParams }) => {
      const { id, ...body } = args;
      const { data, error } = await apiClient.PUT("/net-worth/forecast/presets/{id}", {
        params: { path: { id } },
        body,
      });
      throwIfError(error);
      return data as SavedPreset;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: PRESETS_KEY });
    },
  });
}

export function useDeletePreset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => {
      const { error } = await apiClient.DELETE("/net-worth/forecast/presets/{id}", {
        params: { path: { id } },
      });
      throwIfError(error);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: PRESETS_KEY });
    },
  });
}
