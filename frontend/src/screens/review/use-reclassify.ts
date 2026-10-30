import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { useToast } from "@/components/ui/use-toast";

import { fetchJobStatus, startReclassify } from "./api";

const POLL_MS = 750;

export interface UseReclassifyResult {
  /** Starts the re-run (does nothing while one is already in flight). */
  start: () => void;
  /** True from the click until the job reaches a terminal state. */
  running: boolean;
}

/**
 * "Re-classify uncategorized": re-runs the cascade (`POST /classify/run`, `only_unclassified`) over
 * the rows that are uncategorized or still flagged for review — a new rule or a changed LLM setup
 * only helps rows that were already imported if something asks for them to be reconsidered. Polls
 * the job until it finishes, then tells the user and calls `onDone` so the queue can reload.
 * `userId` is the selected user (`null` = every user).
 */
export function useReclassify(userId: number | null, onDone: () => void): UseReclassifyResult {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [jobId, setJobId] = React.useState<string | null>(null);
  const [count, setCount] = React.useState(0);
  // The latest `onDone` without making the effect below re-run for a new closure.
  const onDoneRef = React.useRef(onDone);
  onDoneRef.current = onDone;

  const starter = useMutation({
    mutationFn: () => startReclassify(userId),
    onSuccess: ({ jobId: startedJobId, transactionCount }) => {
      setCount(transactionCount);
      if (transactionCount === 0) {
        toast({ title: "Nothing to re-classify", description: "No transaction is uncategorized." });
        return;
      }
      setJobId(startedJobId);
    },
    onError: (error) => {
      toast({
        title: "Could not start re-classifying",
        description: error instanceof Error ? error.message : undefined,
        variant: "destructive",
      });
    },
  });

  const job = useQuery({
    queryKey: ["review", "reclassify-job", jobId],
    enabled: jobId !== null,
    queryFn: () => fetchJobStatus(jobId as string),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "done" || status === "error" || status === "cancelled" ? false : POLL_MS;
    },
  });

  const status = job.data?.status;
  React.useEffect(() => {
    if (jobId === null) return;
    if (status !== "done" && status !== "error" && status !== "cancelled") return;
    setJobId(null);
    if (status === "done") {
      toast({
        title: `Re-classified ${count} transaction${count === 1 ? "" : "s"}`,
        description: "Your own categorizations were left as they are.",
        variant: "success",
      });
    } else {
      toast({
        title: "Re-classifying did not finish",
        description: job.data?.error_detail ?? undefined,
        variant: "destructive",
      });
    }
    void queryClient.invalidateQueries();
    onDoneRef.current();
  }, [jobId, status, count, job.data?.error_detail, queryClient, toast]);

  return {
    start: () => {
      if (!starter.isPending && jobId === null) starter.mutate();
    },
    running: starter.isPending || jobId !== null,
  };
}
