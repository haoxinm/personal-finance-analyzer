import * as React from "react";
import { Link } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useJobProgress, type JobEvent } from "@/hooks/use-job-progress";
import { Money } from "@/components/primitives/money";

import { isSkipConfirmForIssuer, setSkipConfirmForIssuer } from "./storage";
import { isConfidentProposal } from "./use-import-queue";
import type { ImportItem } from "./types";

export interface StatementCardProps {
  item: ImportItem;
  onJobEvent: (clientId: string, event: JobEvent) => void;
  onRememberChange: (remember: boolean) => void;
  onConfirm: () => void;
  /** The issuer/extractor override UI, pre-wired by the caller (`IssuerExtractorPicker`) —
   * kept out of this component so it stays a pure presentational shell around the state
   * machine's status/progress/outcome rendering. */
  picker: React.ReactNode;
  /** Page selection + LLM privacy notice (`PageSelector`), shown under the picker. */
  pages?: React.ReactNode;
}

const STATUS_LABEL: Record<ImportItem["status"], string> = {
  uploading: "Uploading…",
  upload_failed: "Upload failed",
  awaiting_extractor: "Ready to import",
  extracting: "Importing…",
  parsed: "Imported",
  no_text_layer: "Can't read this PDF",
  unsupported_layout: "Unrecognized layout",
  error: "Import failed",
};

const STATUS_BADGE_VARIANT: Record<ImportItem["status"], "default" | "secondary" | "destructive" | "outline"> = {
  uploading: "secondary",
  upload_failed: "destructive",
  awaiting_extractor: "outline",
  extracting: "secondary",
  parsed: "default",
  no_text_layer: "destructive",
  unsupported_layout: "destructive",
  error: "destructive",
};

function parseShapeWarnings(raw: string | null | undefined): string[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((w): w is string => typeof w === "string") : [];
  } catch {
    return [];
  }
}

/** The outcome line for a parsed item. A re-dropped file that was already imported says so
 * (its statement's `txn_count` is the earlier import's, not new rows); otherwise "N new", plus
 * how many rows an overlapping statement had already provided. */
function parsedOutcomeText(item: ImportItem): string {
  const count = item.statement?.txn_count ?? 0;
  if (item.duplicate) {
    return `Already imported — ${count} transaction${count === 1 ? "" : "s"} on file, nothing new`;
  }
  const inserted = item.insertedCount ?? count;
  const parts = [`${inserted} new transaction${inserted === 1 ? "" : "s"}`];
  if (item.skippedDuplicates) parts.push(`${item.skippedDuplicates} already present`);
  if (item.needsReviewCount !== undefined) parts.push(`${item.needsReviewCount} need review`);
  return parts.join(" · ");
}

/** One row of the Import screen's per-file list: status, live progress, the issuer/extractor
 * picker while `awaiting_extractor`, and the outcome (success with reconciliation, or one of
 * the four failure shapes the plan names) once the extract job finishes. */
export function StatementCard({
  item,
  onJobEvent,
  onRememberChange,
  onConfirm,
  picker,
  pages,
}: StatementCardProps) {
  const { event } = useJobProgress(item.status === "extracting" ? item.jobId : null);

  React.useEffect(() => {
    if (event) onJobEvent(item.clientId, event);
    // `onJobEvent` is expected to be referentially stable per render pass of the parent list;
    // re-running only when the event itself (or which item this is) changes avoids re-dispatching
    // the same event on every unrelated parent re-render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [event, item.clientId]);

  const canConfirm =
    item.issuerId !== null && item.extractorChoice !== null && item.selectedPages?.length !== 0;
  const shapeWarnings = parseShapeWarnings(item.statement?.shape_warnings);
  const reconciliationDelta = item.statement?.reconciliation_delta_minor ?? null;

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-foreground" title={item.fileName}>
              {item.fileName}
            </p>
            {item.status === "extracting" && item.jobEvent ? (
              <p className="text-xs text-muted-foreground">
                {item.jobEvent.message ?? `${Math.round(item.jobEvent.progress * 100)}%`}
              </p>
            ) : null}
          </div>
          <Badge variant={STATUS_BADGE_VARIANT[item.status]} className="shrink-0">
            {STATUS_LABEL[item.status]}
          </Badge>
        </div>

        {item.status === "extracting" ? (
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" role="progressbar">
            <div
              className="h-full bg-accent-foreground transition-all"
              style={{ width: `${Math.round((item.jobEvent?.progress ?? 0) * 100)}%` }}
            />
          </div>
        ) : null}

        {item.status === "awaiting_extractor" ? (
          <div className="flex flex-col gap-3">
            {item.failure ? (
              <div
                role="alert"
                data-testid="parser-warning"
                className="flex flex-col gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm text-foreground"
              >
                <p>
                  {item.failure.previous
                    ? `A previous attempt to import this statement failed: ${item.failure.detail}. Choose an extractor (change your settings first if needed) and import again.`
                    : item.failure.llm
                    ? `The LLM parser couldn't read this statement: ${item.failure.detail}. Adjust the selected pages or choose another extractor, then import again.`
                    : `The built-in parser couldn't read this statement: ${item.failure.detail}. Choose the LLM parser (or another extractor) and import again.`}
                </p>
                <Link
                  to={`/spending/layout-mapper?statement_id=${item.statementId ?? ""}`}
                  className="self-start text-xs underline"
                >
                  Map this layout
                </Link>
              </div>
            ) : null}
            {item.errorMessage ? (
              <p className="text-sm text-destructive">{item.errorMessage}</p>
            ) : null}
            {item.proposal ? (
              <p className="text-xs text-muted-foreground">
                Detected confidence: {Math.round(item.proposal.confidence * 100)}%
                {isConfidentProposal(item) ? " — confident match" : ""}
              </p>
            ) : null}
            {picker}
            {pages}
            <div className="flex flex-wrap items-center gap-4">
              <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={item.remember}
                  onChange={(event_) => onRememberChange(event_.target.checked)}
                />
                Remember this extractor for this issuer
              </label>
              {item.issuerId !== null ? (
                <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    defaultChecked={isSkipConfirmForIssuer(item.issuerId)}
                    onChange={(event_) => {
                      if (item.issuerId !== null) {
                        setSkipConfirmForIssuer(item.issuerId, event_.target.checked);
                      }
                    }}
                  />
                  Don&rsquo;t ask again for this issuer
                </label>
              ) : null}
              <Button
                type="button"
                size="sm"
                className="ml-auto"
                disabled={!canConfirm}
                onClick={onConfirm}
              >
                Import
              </Button>
            </div>
          </div>
        ) : null}

        {item.status === "no_text_layer" || item.status === "upload_failed" || item.status === "error" ? (
          <p className="text-sm text-destructive">{item.errorMessage}</p>
        ) : null}

        {item.status === "unsupported_layout" ? (
          <div className="flex flex-col gap-2">
            <p className="text-sm text-destructive">
              No layout parser matched this statement&rsquo;s format.
            </p>
            <Button asChild size="sm" variant="outline" className="self-start">
              <Link to={`/spending/layout-mapper?statement_id=${item.statementId ?? ""}`}>
                Map this layout
              </Link>
            </Button>
          </div>
        ) : null}

        {item.status === "parsed" ? (
          <div className="flex flex-col gap-1 text-sm">
            <p className="text-foreground">{parsedOutcomeText(item)}</p>
            {reconciliationDelta !== null && reconciliationDelta !== 0 ? (
              <p className="flex items-center gap-1 text-xs text-destructive" role="alert">
                Reconciliation delta: <Money minorUnits={reconciliationDelta} />
              </p>
            ) : null}
            {shapeWarnings.length > 0 ? (
              <p className="text-xs text-destructive">
                This statement parsed differently from previous ones from this account — please
                spot-check before trusting the numbers ({shapeWarnings.join("; ")}).
              </p>
            ) : null}
            <div className="flex flex-wrap items-center gap-2 pt-1">
              {!item.duplicate && (item.needsReviewCount ?? 0) > 0 ? (
                <Button asChild size="sm">
                  <Link to="/spending/review">Review {item.needsReviewCount} &rarr;</Link>
                </Button>
              ) : null}
              <Button asChild size="sm" variant="outline">
                <Link to={`/spending/transactions?statement_id=${item.statementId ?? ""}`}>
                  View transactions
                </Link>
              </Button>
            </div>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
