import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { useToast } from "@/components/ui/use-toast";

import { apiClient, throwIfError, type components } from "./client";

export type TransactionCandidate = components["schemas"]["TransactionCandidate"];
export type TransactionCandidates = components["schemas"]["TransactionCandidates"];
type UndoToken = components["schemas"]["AcceptUndo"];

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
 * Refresh-and-undo plumbing shared by every action that hands back an undo token
 * (`POST /review/undo`: an accepted suggestion, a revert to automatic).
 *
 * `undo` is a plain function, not a mutation: the toast's Undo button outlives the component that
 * made the change (the edit dialog closes), and must still work after it unmounts.
 */
function useUndoToken() {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const refresh = React.useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["transactions"] });
  }, [queryClient]);

  const undo = React.useCallback(
    async (token: UndoToken) => {
      try {
        const { data, error } = await apiClient.POST("/review/undo", { body: token });
        throwIfError(error);
        refresh();
        // The server restores a row only if it still is what the change left it as: a row edited
        // since stays as the user set it and is reported here instead of being overwritten.
        const skipped = data?.skipped?.length ?? 0;
        const notes: string[] = [];
        if (skipped > 0) {
          notes.push(
            `${skipped} transaction${skipped === 1 ? " was" : "s were"} edited since, so ${skipped === 1 ? "it was" : "they were"} left as you set ${skipped === 1 ? "it" : "them"}.`,
          );
        }
        if (data?.rule_skipped) notes.push("The rule was changed since, so it was left alone.");
        toast({
          title: data !== undefined && data.restored === 0 ? "Nothing to undo" : "Undone",
          description: notes.length > 0 ? notes.join(" ") : undefined,
        });
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

  return { refresh, undo };
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
  const { toast } = useToast();
  const { refresh, undo } = useUndoToken();

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

/**
 * Hands one-time-edited transactions back to automatic classification, with an Undo on the toast.
 *
 * `POST /transactions/{id}/revert-to-automatic` (one row) or `/transactions/bulk-revert-to-automatic`
 * (several) clears the edit and re-runs the rules, saved merchant answers and issuer labels for the
 * row; the server never calls an LLM for this. If nothing matches, the row becomes Uncategorized and
 * is flagged for review. The undo token is the same one an accepted suggestion returns, so Undo
 * restores the edit exactly.
 */
export function useRevertToAutomatic() {
  const { toast } = useToast();
  const { refresh, undo } = useUndoToken();

  return useMutation({
    mutationFn: async ({ transactionIds }: { transactionIds: number[] }) => {
      if (transactionIds.length === 1) {
        const { data, error } = await apiClient.POST("/transactions/{id}/revert-to-automatic", {
          params: { path: { id: transactionIds[0]! } },
        });
        throwIfError(error);
        return {
          reverted: 1,
          skipped: 0,
          undo: data!.undo,
          needsReview: data!.transaction.needs_review,
        };
      }
      const { data, error } = await apiClient.POST("/transactions/bulk-revert-to-automatic", {
        body: { transaction_ids: transactionIds },
      });
      throwIfError(error);
      return { ...data!, needsReview: false };
    },
    onSuccess: (result) => {
      refresh();
      const skipped =
        result.skipped > 0
          ? ` ${result.skipped.toLocaleString()} had no one-time edit and ${result.skipped === 1 ? "was" : "were"} left as is.`
          : "";
      if (result.reverted === 0) {
        toast({ title: "Nothing to revert", description: `None of these were edited by hand.${skipped}` });
        return;
      }
      toast({
        title:
          result.reverted === 1
            ? "Reverted to automatic"
            : `Reverted ${result.reverted.toLocaleString()} transactions to automatic`,
        description: `Rules decide ${result.reverted === 1 ? "this one" : "these"} again.${
          result.needsReview ? " Nothing matched, so it is flagged for review." : ""
        }${skipped}`,
        variant: "success",
        duration: 8000,
        action: { label: "Undo", onClick: () => void undo(result.undo) },
      });
    },
    onError: (error: unknown) => {
      toast({
        title: "Could not revert to automatic",
        description: error instanceof Error ? error.message : undefined,
        variant: "destructive",
      });
    },
  });
}
