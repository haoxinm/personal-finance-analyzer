import * as React from "react";

import type { components } from "@/api/client";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/use-toast";
import { useJobProgress } from "@/hooks/use-job-progress";

import { useTestLayoutSpec } from "./api";

type Statement = components["schemas"]["Statement"];

/**
 * Runs a saved (not-yet-approved is fine — neither `/extract` nor `/reparse` require it) spec
 * against the real statement, actually parsing and persisting it — unlike `DryRunPanel`'s
 * side-effect-free `/layout-specs/dry-run`, this is the real thing: `POST
 * /statements/{id}/extract` (statement not yet parsed) or `/reparse` (already parsed — "rebuilds
 * that statement's rows", §3.12). Use `DryRunPanel` while iterating; use this once the spec is
 * saved and ready to actually import.
 */
export function TestExtractPanel({
  statement,
  layoutSpecId,
  issuerId,
}: {
  statement: Statement;
  layoutSpecId: number | null;
  issuerId: number | null;
}) {
  const { toast } = useToast();
  const testSpec = useTestLayoutSpec();
  const [jobId, setJobId] = React.useState<string | null>(null);
  const { event } = useJobProgress(jobId);

  const alreadyParsed = statement.status === "parsed";
  const canRun = layoutSpecId != null && issuerId != null;

  async function run(): Promise<void> {
    if (!canRun || layoutSpecId == null || issuerId == null) return;
    try {
      const result = await testSpec.mutateAsync({
        statementId: statement.id,
        layoutSpecId,
        issuerId,
        alreadyParsed,
      });
      if (result?.job_id) setJobId(result.job_id);
    } catch (exc) {
      toast({ title: "Could not run this spec", description: String(exc), variant: "destructive" });
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border p-3">
      <h3 className="text-sm font-semibold">Test this spec on the statement</h3>
      <p className="text-xs text-muted-foreground">
        Save the spec first, then pick it as "revise an existing spec" or note its id above. This actually{" "}
        {alreadyParsed ? "rebuilds this statement's transactions" : "parses and persists this statement's transactions"}{" "}
        with the saved spec — it is not a preview. Reconciliation delta and transaction count below come from the
        real result.
      </p>
      <Button size="sm" onClick={() => void run()} disabled={!canRun || testSpec.isPending}>
        Run
      </Button>
      {!canRun ? (
        <p className="text-xs text-muted-foreground">Save the spec above first to get its id, then select it here.</p>
      ) : null}
      {event ? (
        <p className="text-xs">
          Job {event.status}
          {event.status === "error" && event.error_detail ? `: ${event.error_detail}` : null}
        </p>
      ) : null}
      {event?.status === "done" && (event.unmatched_edits ?? 0) > 0 ? (
        <p role="alert" className="text-xs font-medium text-destructive">
          {event.unmatched_edits} of your hand edits (category, kind or notes) could not be matched to a
          rebuilt transaction and {event.unmatched_edits === 1 ? "was" : "were"} dropped. Check the rows you
          corrected on this statement.
        </p>
      ) : event?.status === "done" && (event.restored_edits ?? 0) > 0 ? (
        <p className="text-xs">
          Kept {event.restored_edits} hand edit{event.restored_edits === 1 ? "" : "s"} on the rebuilt rows.
        </p>
      ) : null}
      {statement.txn_count != null ? (
        <p className="text-xs font-medium">
          {statement.txn_count} transaction(s)
          {statement.reconciliation_delta_minor != null
            ? `, reconciliation delta ${(statement.reconciliation_delta_minor / 100).toFixed(2)}`
            : ""}
        </p>
      ) : null}
    </div>
  );
}
