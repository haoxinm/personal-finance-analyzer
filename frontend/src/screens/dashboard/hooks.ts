import * as React from "react";
import { useQuery } from "@tanstack/react-query";

import { useStatements } from "@/api/hooks";
import { apiClient, throwIfError } from "@/api/client";

/** The subset of `SpendQuery` (§3.9) every dashboard chart shares: who, when, and which
 * currency to filter to (D5: never converted). Each hook below adds its own `group_by` /
 * `granularity` / `limit` on top, since those differ per chart and per `/api/analytics/*`
 * route (§3.12). */
export interface DashboardFilters {
  userIds?: number[];
  dateFrom?: string;
  dateTo?: string;
  currency?: string;
}

type Granularity = "day" | "week" | "month" | "quarter" | "year" | "all";

/** All accounts, used only to detect other currencies in scope (D5's banner) — the dashboard
 * does not offer per-account filtering in v1. */
export function useAccounts() {
  return useQuery({
    queryKey: ["accounts"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/accounts");
      throwIfError(error);
      return data ?? [];
    },
  });
}

/** `GET /api/analytics/summary` grouped by `["period", "category"]` — feeds the stacked bar. */
export function useCategoryBreakdown(filters: DashboardFilters, granularity: Granularity, enabled = true) {
  return useQuery({
    enabled,
    queryKey: ["dashboard", "category-breakdown", filters, granularity],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/analytics/summary", {
        params: {
          query: {
            user_ids: filters.userIds,
            date_from: filters.dateFrom,
            date_to: filters.dateTo,
            currency: filters.currency,
            granularity,
            group_by: ["period", "category"],
          },
        },
      });
      throwIfError(error);
      return data ?? [];
    },
  });
}

/** `GET /api/analytics/summary` grouped by `["user"]` — feeds the user-comparison panel, only
 * fetched (`enabled`) when more than one user exists (P3-D's brief). */
export function useUserBreakdown(filters: DashboardFilters, enabled: boolean) {
  return useQuery({
    queryKey: ["dashboard", "user-breakdown", filters],
    enabled,
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/analytics/summary", {
        params: {
          query: {
            user_ids: filters.userIds,
            date_from: filters.dateFrom,
            date_to: filters.dateTo,
            currency: filters.currency,
            granularity: "all",
            group_by: ["user"],
          },
        },
      });
      throwIfError(error);
      return data ?? [];
    },
  });
}

/** `GET /api/analytics/timeseries` grouped by `["period"]` — feeds the trend line and the
 * month-over-month delta. */
export function useTrend(filters: DashboardFilters, granularity: Granularity, enabled = true) {
  return useQuery({
    enabled,
    queryKey: ["dashboard", "trend", filters, granularity],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/analytics/timeseries", {
        params: {
          query: {
            user_ids: filters.userIds,
            date_from: filters.dateFrom,
            date_to: filters.dateTo,
            currency: filters.currency,
            granularity,
            group_by: ["period"],
          },
        },
      });
      throwIfError(error);
      return data ?? [];
    },
  });
}

/** `GET /api/analytics/top-merchants`, ranked server-side (§3.12). */
export function useTopMerchants(filters: DashboardFilters, limit = 8, enabled = true) {
  return useQuery({
    enabled,
    queryKey: ["dashboard", "top-merchants", filters, limit],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/analytics/top-merchants", {
        params: {
          query: {
            user_ids: filters.userIds,
            date_from: filters.dateFrom,
            date_to: filters.dateTo,
            currency: filters.currency,
            limit,
          },
        },
      });
      throwIfError(error);
      return data ?? [];
    },
  });
}

export interface ImportAttention {
  /** Parsed statements carrying an attention flag (reconciliation mismatch, no rows, ...). */
  statements: number;
  /** Files in the watched folder that failed or are not in a folder the app imports from. */
  folderProblems: number;
  total: number;
}

/**
 * What on the Import page needs a look, for the Overview's hint: statements the server flags
 * (`attention`) plus folder-import problems. Quiet by design: a failed lookup counts as zero,
 * the hint is a nudge and must never turn the Overview into an error screen.
 */
export function useImportAttention(userId?: number, enabled = true) {
  const statements = useStatements(userId);
  const problems = useQuery({
    enabled,
    queryKey: ["dashboard", "watch-problems"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/watch/files", {
        params: { query: { view: "problems", limit: 1 } },
      });
      throwIfError(error);
      return data?.total ?? 0;
    },
    refetchInterval: 60_000,
  });
  const attention: ImportAttention = React.useMemo(() => {
    const flagged = (statements.data ?? []).filter(
      (statement) => statement.status === "parsed" && (statement.attention ?? []).length > 0,
    ).length;
    const folder = problems.data ?? 0;
    return { statements: flagged, folderProblems: folder, total: flagged + folder };
  }, [statements.data, problems.data]);
  return { ...attention, isLoading: statements.isLoading || problems.isLoading };
}
