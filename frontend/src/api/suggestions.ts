import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { useToast } from "@/components/ui/use-toast";

import { apiClient, throwIfError, type components } from "./client";

export type TransactionCandidate = components["schemas"]["TransactionCandidate"];
export type TransactionCandidates = components["schemas"]["TransactionCandidates"];

/** `GET /transactions/{id}/candidates`: what each classification layer would say about one
 * transaction (read-only; the server never calls an LLM for this). Disabled without an id. */
export function useTransactionCandidates(id: number | undefined) {
  return useQuery({
    queryKey: ["transactions", "candidates", id],
    enabled: id !== undefined,
    queryFn: async (): Promise<TransactionCandidates> => {
      const { data, error } = await apiClient.GET("/transactions/{id}/candidates", {
        params: { path: { id: id as number } },
      });
      throwIfError(error);
      return data!;
    },
  });
}

/**
 * Applies a suggestion as a one-time edit of one transaction, with an Undo on the toast.
 *
 * It goes through `POST /review/accept` (no rule, no other rows) rather than a bare `PATCH`:
 * that is the same one-time edit (`apply_user_correction`), but it hands back the undo token
 * `POST /review/undo` needs to put the row back exactly as it was (category, provenance, kind
 * and review flag), which a second PATCH could not do. A rule's kind override is passed along.
 */
export function useApplySuggestion() {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const refresh = React.useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["transactions"] });
  }, [queryClient]);

  // A plain function, not a mutation: the toast's Undo button outlives the component that
  // applied the suggestion (the popover closes), and must still work after it unmounts.
  const undo = React.useCallback(
    async (token: components["schemas"]["AcceptUndo"]) => {
      try {
        const { error } = await apiClient.POST("/review/undo", { body: token });
        throwIfError(error);
        refresh();
        toast({ title: "Undone" });
      } catch (error) {
        toast({
          title: "Undo failed",
          description: error instanceof Error ? error.message : undefined,
          variant: "destructive",
        });
      }
    },
    [refresh, toast],
  );

  const apply = useMutation({
    mutationFn: async ({
      transactionId,
      candidate,
    }: {
      transactionId: number;
      candidate: TransactionCandidate;
      /** What the toast calls the chosen category, e.g. "Grocery › Wholesale clubs". */
      label: string;
    }) => {
      const { data, error } = await apiClient.POST("/review/accept", {
        body: {
          transaction_id: transactionId,
          category_key: candidate.category_key,
          subcategory_key: candidate.subcategory_key,
          create_rule: false,
          similar_ids: [],
          ...(candidate.kind ? { kind: candidate.kind } : {}),
        },
      });
      throwIfError(error);
      return data!;
    },
    onSuccess: (data, { label }) => {
      refresh();
      toast({
        title: `Set to ${label}`,
        description: "For this transaction only.",
        variant: "success",
        duration: 8000,
        action: { label: "Undo", onClick: () => void undo(data.undo) },
      });
    },
    onError: (error: unknown) => {
      toast({
        title: "Could not apply that suggestion",
        description: error instanceof Error ? error.message : undefined,
        variant: "destructive",
      });
    },
  });

  return apply;
}
