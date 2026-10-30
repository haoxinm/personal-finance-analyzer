import * as React from "react";

import { useToast } from "@/components/ui/use-toast";

import { acceptReview, fetchNeedsReviewQueue, undoReview } from "./api";
import type { Transaction, UndoEntry } from "./types";

export interface StagedPick {
  categoryKey: string;
  subcategoryKey: string;
}

export interface UseReviewQueueResult {
  status: "loading" | "error" | "ready";
  error: unknown;
  queue: Transaction[];
  index: number;
  current: Transaction | null;
  isMutating: boolean;
  canUndo: boolean;
  /** Moves the cursor to the next transaction in the queue (the `j` key). Clamped at the end. */
  next: () => void;
  /** Moves the cursor to the previous transaction (the `k` key). Clamped at the start. */
  previous: () => void;
  /** Stages a category/subcategory pick for the current transaction, without saving it yet. */
  stage: (categoryKey: string, subcategoryKey: string) => void;
  stagedForCurrent: StagedPick | undefined;
  /** The other queue rows from the same merchant as the current one (same non-empty
   * `merchant_key`): what "apply to the rest" would also categorize. */
  similarForCurrent: Transaction[];
  /** Whether an accept also applies to `similarForCurrent` (default on). */
  applyToSimilar: boolean;
  setApplyToSimilar: (apply: boolean) => void;
  /**
   * Accepts the current transaction: the staged pick if one was made, otherwise whatever
   * category the cascade already proposed — and, when `applyToSimilar` is on, the same for the
   * merchant's other queue rows. Optimistically removes the current row from the queue; on a
   * failed request it is reinserted at its original position and the user is told why (rollback).
   * `createRule` mirrors the `r` key — "always categorize this merchant this way" (§P3-B) — via
   * `POST /review/accept`'s `create_rule`.
   */
  accept: (options?: { createRule?: boolean }) => Promise<void>;
  /**
   * Reverts the single most recent `accept` through the server's undo token: every row it touched
   * returns to its previous category (or to none), `needs_review` flag and kind, and the
   * merchant-map entry and rule it wrote are removed or restored. The rows return to the queue
   * where they were, and survive a reload because the server state is genuinely restored.
   */
  undo: () => Promise<void>;
  refetch: () => void;
}

/**
 * Drives the review queue: loads every `needs_review` transaction (`fetchNeedsReviewQueue`,
 * `api.ts`) and manages the session's local cursor, staged category picks, optimistic
 * accept/undo, and the undo stack that `index.tsx` wires to the `u` key.
 *
 * The queue lives in local component state rather than the TanStack Query cache: once loaded,
 * accept/undo mutate this array directly (removing or reinserting rows) so the visible order and
 * cursor position survive a rollback exactly, which a query-cache `setQueryData` dance over a
 * paginated, client-filtered list would not do cleanly.
 */
export function useReviewQueue(userIds?: number[], enabled = true): UseReviewQueueResult {
  const { toast } = useToast();
  const [queue, setQueue] = React.useState<Transaction[]>([]);
  const [status, setStatus] = React.useState<"loading" | "error" | "ready">("loading");
  const [error, setError] = React.useState<unknown>(null);
  const [index, setIndex] = React.useState(0);
  const [staged, setStaged] = React.useState<Record<number, StagedPick>>({});
  const [isMutating, setIsMutating] = React.useState(false);
  const [canUndo, setCanUndo] = React.useState(false);
  const [applyToSimilar, setApplyToSimilar] = React.useState(true);
  const undoStack = React.useRef<UndoEntry[]>([]);

  // Stable dependency: callers pass a fresh array each render.
  const userKey = userIds?.join(",");

  const load = React.useCallback(() => {
    setStatus("loading");
    setError(null);
    fetchNeedsReviewQueue(userKey === undefined ? undefined : userKey.split(",").map(Number))
      .then((items) => {
        setQueue(items);
        setIndex(0);
        setStaged({});
        undoStack.current = [];
        setCanUndo(false);
        setStatus("ready");
      })
      .catch((caught: unknown) => {
        setError(caught);
        setStatus("error");
      });
  }, [userKey]);

  React.useEffect(() => {
    if (enabled) load();
  }, [load, enabled]);

  // Keep the cursor in range as the queue shrinks (an accept removes its row).
  React.useEffect(() => {
    setIndex((i) => Math.min(i, Math.max(queue.length - 1, 0)));
  }, [queue.length]);

  const current = queue[index] ?? null;

  const similarForCurrent = React.useMemo(
    () =>
      current && current.merchant_key !== ""
        ? queue.filter((t) => t.id !== current.id && t.merchant_key === current.merchant_key)
        : [],
    [queue, current],
  );

  const next = React.useCallback(() => {
    setIndex((i) => Math.min(i + 1, Math.max(queue.length - 1, 0)));
  }, [queue.length]);

  const previous = React.useCallback(() => {
    setIndex((i) => Math.max(i - 1, 0));
  }, []);

  const stage = React.useCallback(
    (categoryKey: string, subcategoryKey: string) => {
      if (!current) return;
      setStaged((prev) => ({ ...prev, [current.id]: { categoryKey, subcategoryKey } }));
    },
    [current],
  );

  const accept = React.useCallback(
    async ({ createRule = false }: { createRule?: boolean } = {}) => {
      const txn = current;
      if (!txn) return;
      const pick = staged[txn.id];
      const categoryKey = pick?.categoryKey ?? txn.category_key;
      const subcategoryKey = pick?.subcategoryKey ?? txn.subcategory_key;
      if (!categoryKey || !subcategoryKey) {
        toast({
          title: "Pick a category first",
          description: "Press a number key to choose one, then Enter or r.",
          variant: "destructive",
        });
        return;
      }

      const snapshot = txn;
      const queueIndexAtAccept = index;
      const similar = applyToSimilar ? similarForCurrent : [];
      const positions = new Map(queue.map((t, position) => [t.id, position]));

      // Optimistic: the row leaves the queue immediately.
      setQueue((prevQueue) => prevQueue.filter((t) => t.id !== txn.id));
      setIsMutating(true);
      try {
        const result = await acceptReview({
          transactionId: txn.id,
          categoryKey,
          subcategoryKey,
          createRule,
          similarIds: similar.map((t) => t.id),
        });
        // The server skips any row that no longer qualifies; only the applied ones leave the queue.
        const applied = new Set(result.similar_ids);
        setQueue((prevQueue) => prevQueue.filter((t) => !applied.has(t.id)));
        const appliedRows = similar.filter((t) => applied.has(t.id));
        undoStack.current = [
          ...undoStack.current,
          {
            undo: result.undo,
            removed: [
              { queueIndex: queueIndexAtAccept, transaction: snapshot },
              ...appliedRows.map((t) => ({ queueIndex: positions.get(t.id) ?? 0, transaction: t })),
            ].sort((a, b) => a.queueIndex - b.queueIndex),
          },
        ];
        setCanUndo(true);
        setStaged((prev) => {
          if (!(txn.id in prev)) return prev;
          const rest = { ...prev };
          delete rest[txn.id];
          return rest;
        });
        toast({
          title: createRule ? "Accepted — rule created" : "Accepted",
          description:
            appliedRows.length > 0
              ? `Applied to ${appliedRows.length} more from this merchant. Press u to undo.`
              : undefined,
          variant: "success",
        });
      } catch (caught) {
        // Rollback: put the row back exactly where it was.
        setQueue((prevQueue) => {
          const restored = [...prevQueue];
          restored.splice(Math.min(queueIndexAtAccept, restored.length), 0, snapshot);
          return restored;
        });
        setIndex(queueIndexAtAccept);
        toast({
          title: "Could not save — restored to the queue",
          description: caught instanceof Error ? caught.message : undefined,
          variant: "destructive",
        });
      } finally {
        setIsMutating(false);
      }
    },
    [current, index, staged, queue, applyToSimilar, similarForCurrent, toast],
  );

  const undo = React.useCallback(async () => {
    const entry = undoStack.current.at(-1);
    if (!entry) return;
    setIsMutating(true);
    try {
      await undoReview(entry.undo);
      undoStack.current = undoStack.current.slice(0, -1);
      setCanUndo(undoStack.current.length > 0);
      setQueue((prevQueue) => {
        // Ascending original positions put each row back where it sat before the accept.
        const restored = [...prevQueue];
        for (const { queueIndex, transaction } of entry.removed) {
          restored.splice(Math.min(queueIndex, restored.length), 0, transaction);
        }
        return restored;
      });
      setIndex(entry.removed[0]?.queueIndex ?? 0);
      toast({
        title: entry.removed.length > 1 ? `Undone — ${entry.removed.length} transactions restored` : "Undone",
      });
    } catch (caught) {
      toast({
        title: "Undo failed",
        description: caught instanceof Error ? caught.message : undefined,
        variant: "destructive",
      });
    } finally {
      setIsMutating(false);
    }
  }, [toast]);

  return {
    status,
    error,
    queue,
    index,
    current,
    isMutating,
    canUndo,
    next,
    previous,
    stage,
    stagedForCurrent: current ? staged[current.id] : undefined,
    similarForCurrent,
    applyToSimilar,
    setApplyToSimilar,
    accept,
    undo,
    refetch: load,
  };
}
