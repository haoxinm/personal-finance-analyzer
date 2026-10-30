import * as React from "react";
import { useSearchParams } from "react-router-dom";

/**
 * The grid-only part of the Transactions URL state: filters and sort that `SpendQueryParams`
 * (shared with the dashboard's analytics, which has no use for them) does not carry. Kept in its
 * own hook so a filtered/sorted grid is still linkable, and so a link from Import or Review can
 * land on "this statement's rows" (`?statement_id=`) or "everything needing review"
 * (`?needs_review=true`). `useSpendQueryParams` only ever rewrites its own keys, so the two
 * hooks share one URL without clobbering each other.
 */
export interface GridParams {
  /** `true`: only rows flagged for review. */
  needsReview?: boolean;
  /** `true`: only rows with no category or parked in `others/uncategorized`. */
  uncategorized?: boolean;
  /** `true`: only rows whose classification sources disagree ("Conflicting suggestions"). */
  conflict?: boolean;
  statementId?: number;
  /** `GET /transactions`'s `sort`: `posted_date` or `amount_minor`, `-` prefix for descending. */
  sort: string;
}

export const DEFAULT_SORT = "-posted_date";

const KEYS = {
  needsReview: "needs_review",
  uncategorized: "uncategorized",
  conflict: "conflict",
  statementId: "statement_id",
  sort: "sort",
} as const;

function parse(searchParams: URLSearchParams): GridParams {
  const statementId = Number(searchParams.get(KEYS.statementId));
  const sort = searchParams.get(KEYS.sort);
  return {
    needsReview: searchParams.get(KEYS.needsReview) === "true" ? true : undefined,
    uncategorized: searchParams.get(KEYS.uncategorized) === "true" ? true : undefined,
    conflict: searchParams.get(KEYS.conflict) === "true" ? true : undefined,
    statementId: Number.isInteger(statementId) && statementId > 0 ? statementId : undefined,
    sort: sort === "posted_date" || sort === "amount_minor" || sort === "-amount_minor" ? sort : DEFAULT_SORT,
  };
}

export function useGridParams(): [GridParams, (next: Partial<GridParams>) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const params = React.useMemo(() => parse(searchParams), [searchParams]);

  const setParams = React.useCallback(
    (next: Partial<GridParams>) => {
      setSearchParams(
        (prev) => {
          const merged = new URLSearchParams(prev);
          const current = { ...parse(prev), ...next };
          for (const urlKey of Object.values(KEYS)) merged.delete(urlKey);
          if (current.needsReview) merged.set(KEYS.needsReview, "true");
          if (current.uncategorized) merged.set(KEYS.uncategorized, "true");
          if (current.conflict) merged.set(KEYS.conflict, "true");
          if (current.statementId !== undefined) merged.set(KEYS.statementId, String(current.statementId));
          if (current.sort !== DEFAULT_SORT) merged.set(KEYS.sort, current.sort);
          return merged;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  return [params, setParams];
}

/** The next `sort` after clicking the header of `key`: a fresh column sorts descending first
 * (newest/largest on top), clicking it again flips it, and clicking the date column from its
 * descending default flips to ascending. */
export function nextSort(current: string, key: "posted_date" | "amount_minor"): string {
  return current === `-${key}` ? key : `-${key}`;
}
