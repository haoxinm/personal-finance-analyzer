import { useMutation, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { apiClient, throwIfError } from "@/api/client";
import { useTaxonomy } from "@/api/hooks";
import { useToast } from "@/components/ui/use-toast";

import type { Category, Subcategory } from "./types";

export interface PendingSubcategoryRow extends Subcategory {
  categoryKey: string;
  categoryLabel: string;
}

export interface MergeTarget {
  key: string;
  id: number;
  label: string;
  categoryLabel: string;
}

export interface UsePendingSubcategoriesResult {
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  /** The `pending_approval`, not-yet-merged subcategories (§3.2) — the "stores" of §P3-B. */
  pending: PendingSubcategoryRow[];
  /** Active subcategories a pending one could be merged into. */
  mergeTargets: MergeTarget[];
  approve: (row: PendingSubcategoryRow) => void;
  merge: (row: PendingSubcategoryRow, into: MergeTarget) => void;
  isBusy: (key: string) => boolean;
  /** Re-fetches the taxonomy the list is derived from. */
  refetch: () => void;
}

const TAXONOMY_KEY = ["taxonomy"];

/**
 * Lists `pending_approval` subcategories (dynamic ones the classifier proposed) and drives their
 * approve/merge actions, with an optimistic remove from the list and a rollback to the prior
 * `["taxonomy"]` cache snapshot if the request fails.
 *
 * `POST /taxonomy/subcategories/{subcategory_id}/approve|merge` (§3.12) takes the subcategory's
 * numeric database id, which `Subcategory.id` (§3.12) now carries.
 */
export function usePendingSubcategories(): UsePendingSubcategoriesResult {
  const taxonomy = useTaxonomy();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const categories: Category[] = React.useMemo(() => taxonomy.data ?? [], [taxonomy.data]);

  const pending: PendingSubcategoryRow[] = React.useMemo(
    () =>
      categories.flatMap((cat) =>
        cat.subcategories
          .filter((sub) => sub.pending && sub.merged_into == null)
          .map((sub) => ({ ...sub, categoryKey: cat.key, categoryLabel: cat.name })),
      ),
    [categories],
  );

  const mergeTargets: MergeTarget[] = React.useMemo(
    () =>
      categories.flatMap((cat) =>
        cat.subcategories
          .filter((sub) => !sub.pending && sub.merged_into == null)
          .map((sub) => ({ key: sub.key, id: sub.id, label: sub.name, categoryLabel: cat.name })),
      ),
    [categories],
  );

  function snapshotAndRemove(key: string): Category[] | undefined {
    const previous = queryClient.getQueryData<Category[]>(TAXONOMY_KEY);
    queryClient.setQueryData<Category[] | undefined>(TAXONOMY_KEY, (prev) =>
      prev?.map((cat) => ({
        ...cat,
        subcategories: cat.subcategories.filter((sub) => sub.key !== key),
      })),
    );
    return previous;
  }

  function rollback(previous: Category[] | undefined) {
    if (previous) queryClient.setQueryData(TAXONOMY_KEY, previous);
  }

  const approveMutation = useMutation({
    mutationFn: async (row: PendingSubcategoryRow) => {
      const { data, error } = await apiClient.POST(
        "/taxonomy/subcategories/{subcategory_id}/approve",
        { params: { path: { subcategory_id: row.id } } },
      );
      throwIfError(error);
      return data;
    },
    onMutate: (row) => ({ previous: snapshotAndRemove(row.key) }),
    onError: (caught, row, context) => {
      rollback(context?.previous);
      toast({
        title: `Could not approve "${row.name}"`,
        description: caught instanceof Error ? caught.message : undefined,
        variant: "destructive",
      });
    },
    onSuccess: (_data, row) => {
      toast({ title: `Approved "${row.name}"`, variant: "success" });
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: TAXONOMY_KEY });
    },
  });

  const mergeMutation = useMutation({
    mutationFn: async ({ row, into }: { row: PendingSubcategoryRow; into: MergeTarget }) => {
      const { data, error } = await apiClient.POST(
        "/taxonomy/subcategories/{subcategory_id}/merge",
        { params: { path: { subcategory_id: row.id } }, body: { into_id: into.id } },
      );
      throwIfError(error);
      return data;
    },
    onMutate: ({ row }) => ({ previous: snapshotAndRemove(row.key) }),
    onError: (caught, { row }, context) => {
      rollback(context?.previous);
      toast({
        title: `Could not merge "${row.name}"`,
        description: caught instanceof Error ? caught.message : undefined,
        variant: "destructive",
      });
    },
    onSuccess: (_data, { row, into }) => {
      toast({ title: `Merged "${row.name}" into "${into.label}"`, variant: "success" });
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: TAXONOMY_KEY });
    },
  });

  return {
    isLoading: taxonomy.isLoading,
    isError: taxonomy.isError,
    error: taxonomy.error,
    pending,
    mergeTargets,
    approve: (row) => approveMutation.mutate(row),
    merge: (row, into) => mergeMutation.mutate({ row, into }),
    refetch: () => void taxonomy.refetch(),
    isBusy: (key) =>
      (approveMutation.isPending && approveMutation.variables?.key === key) ||
      (mergeMutation.isPending && mergeMutation.variables?.row.key === key),
  };
}
