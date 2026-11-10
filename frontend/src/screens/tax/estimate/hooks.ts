import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, apiClient, throwIfError } from "@/api/client";

import type { TaxProfile, TaxProfileIn } from "./logic";

/** Same root as the rest of the tax queries, so a document edit or import refreshes the estimate. */
const ROOT_KEY = ["tax"] as const;

/** An API error that remembers its HTTP status (the estimate answers 422, 409 and 404 differently). */
export class TaxApiError extends ApiError {
  readonly status: number;

  constructor(message: string, detail: unknown, status: number) {
    super(message, detail);
    this.name = "TaxApiError";
    this.status = status;
  }
}

function failWithStatus(error: unknown, status: number | undefined): never {
  try {
    throwIfError(error);
  } catch (e) {
    if (e instanceof ApiError) throw new TaxApiError(e.message, e.detail, status ?? 0);
    throw e;
  }
  throw new Error("The request failed.");
}

/** Why an estimate could not be shown: no parameters for the year (422), the spouses' profiles
 * disagree (409), or anything else. */
export type EstimateFailure = "no-parameters" | "spouse-conflict" | "other";

export function estimateFailure(error: unknown): EstimateFailure {
  if (!(error instanceof TaxApiError)) return "other";
  if (error.status === 422) return "no-parameters";
  if (error.status === 409) return "spouse-conflict";
  return "other";
}

/** The estimate of one user and year. A 4xx is an answer, not a glitch, so it is not retried. */
export function useTaxEstimate(userId: number | null, year: number | null, enabled = true) {
  return useQuery({
    enabled: enabled && userId !== null && year !== null,
    queryKey: [...ROOT_KEY, "estimate", { userId, year }],
    retry: (count, error) =>
      !(error instanceof TaxApiError && error.status < 500) && count < 2,
    queryFn: async () => {
      const { data, error, response } = await apiClient.GET("/tax/estimate", {
        params: { query: { user_id: userId!, tax_year: year! } },
      });
      if (error !== undefined && error !== null) failWithStatus(error, response?.status);
      if (!data) throw new Error("The server returned no estimate.");
      return data;
    },
  });
}

/** The saved tax profile of one user and year; `null` when there is none (a 404 is "not saved yet"). */
export function useTaxProfile(userId: number | null, year: number | null, enabled = true) {
  return useQuery({
    enabled: enabled && userId !== null && year !== null,
    queryKey: [...ROOT_KEY, "profile", { userId, year }],
    queryFn: async (): Promise<TaxProfile | null> => {
      const { data, error, response } = await apiClient.GET("/tax/profiles/{user_id}/{tax_year}", {
        params: { path: { user_id: userId!, tax_year: year! } },
      });
      if (response?.status === 404) return null;
      if (error !== undefined && error !== null) failWithStatus(error, response?.status);
      return data ?? null;
    },
  });
}

export function useSaveTaxProfile(userId: number, year: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: TaxProfileIn): Promise<TaxProfile> => {
      const { data, error, response } = await apiClient.PUT("/tax/profiles/{user_id}/{tax_year}", {
        params: { path: { user_id: userId, tax_year: year } },
        body,
      });
      if (error !== undefined && error !== null) failWithStatus(error, response?.status);
      if (!data) throw new Error("The server returned no profile.");
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ROOT_KEY });
    },
  });
}

export function useDeleteTaxProfile(userId: number, year: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { error, response } = await apiClient.DELETE("/tax/profiles/{user_id}/{tax_year}", {
        params: { path: { user_id: userId, tax_year: year } },
      });
      // Already gone is the state the user asked for.
      if (response?.status === 404) return;
      if (error !== undefined && error !== null) failWithStatus(error, response?.status);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ROOT_KEY });
    },
  });
}
