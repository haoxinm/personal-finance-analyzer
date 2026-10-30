import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient, throwIfError } from "@/api/client";
import type { SpendQueryParams } from "@/hooks/use-spend-query-params";

import type { GridParams } from "./use-grid-params";
import type { Kind } from "./types";

/** Rows fetched per page. Kept well under the 10k-row budget (§6.6) per request so the first
 *  page paints fast; `useTransactionsGrid` appends further pages as the user scrolls or asks
 *  for more, and the local windowing hook (`use-virtual-rows.ts`) keeps the DOM small no
 *  matter how many pages have been appended. */
export const GRID_PAGE_SIZE = 200;

/** The subset of `SpendQueryParams` (§3.9) that `GET /transactions` (§3.12) accepts, mapped
 *  onto that endpoint's own (snake_case) query parameter names. `granularity`, `groupBy` and
 *  `netRefunds` are analytics-only (§3.9) and have no meaning here, so they are dropped. */
export function toTransactionsQuery(filters: SpendQueryParams, grid?: GridParams) {
  return {
    user_ids: filters.userIds,
    account_ids: filters.accountIds,
    date_from: filters.dateFrom,
    date_to: filters.dateTo,
    category_keys: filters.categoryKeys,
    subcategory_keys: filters.subcategoryKeys,
    amount_min_minor: filters.amountMinMinor,
    amount_max_minor: filters.amountMaxMinor,
    kinds: filters.kinds as Kind[] | undefined,
    include_non_spend: filters.includeNonSpend,
    search: filters.search,
    currency: filters.currency,
    // Grid-only filters (`use-grid-params.ts`); omitted when no `grid` is given.
    ...(grid === undefined
      ? {}
      : {
          needs_review: grid.needsReview,
          uncategorized: grid.uncategorized,
          statement_id: grid.statementId,
        }),
  };
}

export function transactionsQueryKey(filters: SpendQueryParams, grid?: GridParams) {
  return ["transactions", "grid", toTransactionsQuery(filters, grid), grid?.sort] as const;
}

/**
 * Paginated fetch of the filtered transaction list, one page per `GET /transactions` call
 * (§3.12). Pages accumulate client-side so the grid can virtualize over everything loaded so
 * far; `fetchNextPage` (wired to a "Load more" affordance and to scrolling near the bottom)
 * appends the next `GRID_PAGE_SIZE` rows without re-fetching what is already shown.
 */
export function useTransactionsGrid(filters: SpendQueryParams, grid: GridParams, enabled = true) {
  const query = toTransactionsQuery(filters, grid);
  return useInfiniteQuery({
    enabled,
    queryKey: transactionsQueryKey(filters, grid),
    initialPageParam: 1,
    queryFn: async ({ pageParam }) => {
      const { data, error } = await apiClient.GET("/transactions", {
        params: { query: { ...query, sort: grid.sort, page: pageParam, page_size: GRID_PAGE_SIZE } },
      });
      throwIfError(error);
      return data!;
    },
    getNextPageParam: (lastPage, pages) => {
      const loaded = pages.reduce((sum, page) => sum + page.items.length, 0);
      return loaded < lastPage.total ? pages.length + 1 : undefined;
    },
  });
}

export interface BulkUpdateInput {
  transactionIds: number[];
  categoryKey?: string | null;
  subcategoryKey?: string | null;
  kind?: Kind | null;
}

/** `POST /transactions/bulk-update` (§3.12): applies one label to an explicit list of ids
 *  (the caller resolves "the filtered selection" into ids before calling this — see
 *  `useAllMatchingIds` for "select everything the current filter matches"). */
export function useBulkUpdateTransactions() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: BulkUpdateInput) => {
      const { data, error } = await apiClient.POST("/transactions/bulk-update", {
        body: {
          transaction_ids: input.transactionIds,
          category_key: input.categoryKey ?? undefined,
          subcategory_key: input.subcategoryKey ?? undefined,
          kind: input.kind ?? undefined,
        },
      });
      throwIfError(error);
      return data!;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["transactions"] });
    },
  });
}

/**
 * Resolves "every transaction the current filter matches" into an id list, for "select all N"
 * beyond what the grid has paged in so far: one `GET /transactions/ids` request carrying the same
 * filters as the grid (the list endpoint itself caps a page at 500 rows, so asking it for more
 * silently returned only 500).
 */
export function useAllMatchingIds() {
  return useMutation({
    mutationFn: async ({ filters, grid }: { filters: SpendQueryParams; grid: GridParams }) => {
      const { data, error } = await apiClient.GET("/transactions/ids", {
        params: { query: { ...toTransactionsQuery(filters, grid), sort: grid.sort } },
      });
      throwIfError(error);
      return data!.ids;
    },
  });
}

export interface AccountOption {
  id: number;
  userId: number;
  label: string;
}

/**
 * The accounts the account filter offers, labelled "<issuer> · <type>" (the API deliberately
 * exposes no account number); two accounts that would read the same get a `#id` suffix.
 */
export function useAccountOptions() {
  return useQuery({
    queryKey: ["transactions", "account-options"],
    staleTime: 60_000,
    queryFn: async (): Promise<AccountOption[]> => {
      const [accounts, issuers] = await Promise.all([
        apiClient.GET("/accounts"),
        apiClient.GET("/issuers"),
      ]);
      throwIfError(accounts.error);
      throwIfError(issuers.error);
      const issuerNames = new Map((issuers.data ?? []).map((issuer) => [issuer.id, issuer.name]));
      const labelled = (accounts.data ?? []).map((account) => ({
        id: account.id,
        userId: account.user_id,
        label: `${(account.issuer_id !== null ? issuerNames.get(account.issuer_id) : undefined) ?? "Account"} · ${account.account_type}`,
      }));
      const counts = new Map<string, number>();
      for (const option of labelled) counts.set(option.label, (counts.get(option.label) ?? 0) + 1);
      return labelled.map((option) =>
        (counts.get(option.label) ?? 0) > 1 ? { ...option, label: `${option.label} #${option.id}` } : option,
      );
    },
  });
}

/**
 * Downloads the transactions the grid is showing (`GET /export` takes the same filters as
 * `GET /transactions`, including the selected user view) as a file, entirely client-side (no
 * server-rendered download link to keep the per-launch token attached, A31). Called with no
 * filters it asks for exactly the grid's defaults; the server's own default (everything,
 * every kind and currency) is for the CLI.
 */
export async function downloadTransactionsExport(
  format: "csv" | "json" = "csv",
  filters: SpendQueryParams = {},
  grid?: GridParams,
): Promise<void> {
  const { data, error } = await apiClient.GET("/export", {
    params: {
      query: {
        ...toTransactionsQuery(filters, grid),
        include_non_spend: filters.includeNonSpend ?? false,
        currency: filters.currency ?? "USD",
        format,
      },
    },
    parseAs: "text",
  });
  throwIfError(error);
  const text = data ?? "";
  const mime = format === "csv" ? "text/csv" : "application/json";
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = `transactions.${format}`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  } finally {
    URL.revokeObjectURL(url);
  }
}
