import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import * as React from "react";

import type { components } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useJobProgress } from "@/hooks/use-job-progress";

import { fetchJob } from "./api";
import { useReparseStatement, type ReparseRequest } from "./imported-statements-api";
import { describeReimport, extractorDisplayName } from "./statement-format";
import { errorMessage, useSafeToast } from "./use-safe-toast";

type Statement = components["schemas"]["Statement"];
type LayoutSpec = components["schemas"]["LayoutSpec"];
type Job = components["schemas"]["Job"];



export interface ReimportPanelProps {
  statement: Statement;
  specs: readonly LayoutSpec[];
  llmConfigured: boolean;
  onClose: () => void;
}

const TERMINAL = new Set(["done", "error", "cancelled"]);
const POLL_MS = 1000;
const POLL_MAX = 90;

/** Poll a job (and the classification job it spawned) to the end, refreshing everything that shows
 * imported data after each. Used when the panel is left while work is still running, so the
 * categories still appear without the user doing anything. Never throws. */
async function followJobsThenRefresh(queryClient: QueryClient, jobId: string): Promise<void> {
  let next: string | null | undefined = jobId;
  while (next) {
    let job: Job | null = null;
    for (let attempt = 0; attempt < POLL_MAX; attempt += 1) {
      try {
        job = await fetchJob(next);
      } catch {
        job = null;
      }
      if (job && TERMINAL.has(job.status)) break;
      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    }
    void queryClient.invalidateQueries();
    next = job?.classify_job_id;
    if (next === jobId) return;
  }
}

/**
 * The inline "Re-import" panel under a statement row: rebuild the statement from its stored PDF,
 * optionally with another extractor. Hand edits are kept unless the user opts to discard them; the
 * job's result says how many came back, how many could not be matched and how many were discarded.
 * Whatever happens to the job (done, failed, result unreadable) the panel ends in a state the user
 * can leave or retry from, and everything that shows imported data is refreshed.
 */
export function ReimportPanel({ statement, specs, llmConfigured, onClose }: ReimportPanelProps) {
  const queryClient = useQueryClient();
  const toast = useSafeToast();
  const reparse = useReparseStatement();
  const [choice, setChoice] = React.useState("same");
  const [discard, setDiscard] = React.useState(false);
  const [jobId, setJobId] = React.useState<string | null>(null);
  const [submitError, setSubmitError] = React.useState<string | null>(null);
  const [result, setResult] = React.useState<Job | null>(null);
  const [classifyJobId, setClassifyJobId] = React.useState<string | null>(null);
  const { event } = useJobProgress(jobId);
  const { event: classifyEvent } = useJobProgress(classifyJobId);
  const headingRef = React.useRef<HTMLHeadingElement>(null);

  const titleId = `reimport-title-${statement.id}`;
  const issuerSpecs = specs.filter((s) => s.approved && s.issuer_id === statement.issuer_id);
  const recordedSpec = statement.layout_spec_id != null ? specs.find((s) => s.id === statement.layout_spec_id) : undefined;
  const recordedIsLlm = statement.parser_id === "llm";
  const sameLabel = recordedSpec
    ? `${recordedSpec.name} (v${recordedSpec.version})`
    : extractorDisplayName(statement.parser_id);
  const showLlm = llmConfigured && !recordedIsLlm;
  const jobEnded = event?.status === "done" || event?.status === "error";
  const running = reparse.isPending || (jobId !== null && !jobEnded);
  const jobFailed = event?.status === "error";
  const classifying = classifyJobId !== null && classifyEvent?.status !== "done" && classifyEvent?.status !== "error";

  React.useEffect(() => {
    headingRef.current?.focus();
  }, []);

  // Refs read by the unmount cleanup: was something still running when the panel was left?
  const live = React.useRef<{ jobId: string | null; ended: boolean; classifyId: string | null; classifyEnded: boolean }>({
    jobId: null,
    ended: false,
    classifyId: null,
    classifyEnded: false,
  });
  live.current = {
    jobId,
    ended: jobEnded,
    classifyId: classifyJobId,
    classifyEnded: classifyEvent?.status === "done" || classifyEvent?.status === "error",
  };
  React.useEffect(
    () => () => {
      const { jobId: id, ended, classifyId, classifyEnded } = live.current;
      if (classifyId && !classifyEnded) void followJobsThenRefresh(queryClient, classifyId);
      else if (id && !ended) void followJobsThenRefresh(queryClient, id);
    },
    [queryClient],
  );

  // The re-import job ended: always refresh; on success read its result once (never get stuck if
  // that read fails) and start following the classification job it spawned.
  const finishedFor = React.useRef<string | null>(null);
  React.useEffect(() => {
    if (jobId === null || !jobEnded || finishedFor.current === jobId) return;
    finishedFor.current = jobId;
    void queryClient.invalidateQueries();
    if (event?.status !== "done") return;
    void fetchJob(jobId)
      .then((job) => {
        if (job === null) {
          setSubmitError(
            "The re-import finished, but its result could not be read. Your transactions were updated; check them before importing again.",
          );
          return;
        }
        setResult(job);
        if (job.classify_job_id) setClassifyJobId(job.classify_job_id);
        const { headline, notes } = describeReimport(job);
        toast({
          title: headline,
          description: notes.join(" ") || undefined,
          variant: notes.length > 0 ? "default" : "success",
          duration: 10000,
        });
      })
      .catch(() => {
        setSubmitError(
          "The re-import finished, but its result could not be read. Your transactions were updated; check them before importing again.",
        );
      });
  }, [jobId, jobEnded, event?.status, queryClient, toast]);

  // The classification job ended: the new rows now have categories.
  const classifiedFor = React.useRef<string | null>(null);
  React.useEffect(() => {
    if (classifyJobId === null || classifyEvent === null) return;
    if (classifyEvent.status !== "done" && classifyEvent.status !== "error") return;
    if (classifiedFor.current === classifyJobId) return;
    classifiedFor.current = classifyJobId;
    void queryClient.invalidateQueries();
  }, [classifyJobId, classifyEvent, queryClient]);

  const start = () => {
    const body: ReparseRequest = { discard_edits: discard, remember: false, allow_empty: false };
    if (choice === "llm") body.parser_id = "llm";
    else if (choice.startsWith("spec:")) body.layout_spec_id = Number(choice.slice(5));
    setSubmitError(null);
    setResult(null);
    setJobId(null);
    setClassifyJobId(null);
    reparse.mutateAsync({ statementId: statement.id, body }).then(
      (id) => setJobId(id),
      (error: unknown) => setSubmitError(errorMessage(error)),
    );
  };

  const outcome = result ? describeReimport(result) : null;
  const progress = Math.round((event?.progress ?? 0) * 100);

  return (
    <div
      role="region"
      aria-labelledby={titleId}
      className="flex flex-col gap-3 rounded-md border border-border bg-muted/30 p-3 text-sm"
      data-testid="reimport-panel"
      onKeyDown={(e) => {
        if (e.key === "Escape" && !running) {
          e.stopPropagation();
          onClose();
        }
      }}
    >
      <h4 id={titleId} ref={headingRef} tabIndex={-1} className="font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring">
        Re-import {statement.original_name || `statement ${statement.id}`}
      </h4>
      <p className="text-xs text-muted-foreground">
        Rebuilds this statement&rsquo;s transactions from the saved PDF. Your category edits, notes and corrections
        to dates, amounts and descriptions are kept unless you choose to discard them.
      </p>

      {outcome === null ? (
        <>
          <label className="flex max-w-md flex-col gap-1 text-xs font-medium text-muted-foreground">
            Extractor
            <select
              className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground"
              value={choice}
              onChange={(e) => setChoice(e.target.value)}
              disabled={running}
            >
              <option value="same">Same as before ({sameLabel})</option>
              {issuerSpecs
                .filter((s) => s.id !== statement.layout_spec_id)
                .map((s) => (
                  <option key={s.id} value={`spec:${s.id}`}>
                    {s.name} (v{s.version})
                  </option>
                ))}
              {showLlm ? <option value="llm">LLM extractor</option> : null}
            </select>
          </label>
          {choice === "llm" || (choice === "same" && recordedIsLlm && recordedSpec === undefined) ? (
            <p className="text-xs text-amber-700 dark:text-amber-400">
              Sends this statement&rsquo;s page text (after redaction) to the configured language model.
            </p>
          ) : null}

          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 accent-primary"
              checked={discard}
              onChange={(e) => setDiscard(e.target.checked)}
              disabled={running}
            />
            <span>
              Discard my edits to this statement&rsquo;s transactions
              <span className="block text-xs text-muted-foreground">
                Use this when the edits were made on top of a bad reading.
              </span>
            </span>
          </label>
        </>
      ) : null}

      {running ? (
        <div className="flex flex-col gap-1" aria-live="polite">
          <div
            role="progressbar"
            aria-label="Re-import progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={progress}
            className="h-2 overflow-hidden rounded bg-muted"
          >
            <div className="h-full bg-primary transition-all" style={{ width: `${Math.max(progress, 5)}%` }} />
          </div>
          <span className="text-xs text-muted-foreground">{event?.message ?? "Re-importing…"}</span>
        </div>
      ) : null}

      {submitError ? (
        <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-destructive">
          {submitError}
        </p>
      ) : null}
      {jobFailed ? (
        <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-destructive">
          {event?.error_detail ?? "The re-import failed."}
        </p>
      ) : null}

      {outcome ? (
        <div className="flex flex-col gap-1" data-testid="reimport-result">
          <p className="font-medium">{outcome.headline}</p>
          {outcome.notes.map((note) => (
            <p key={note} className="text-xs text-amber-700 dark:text-amber-400">
              {note}
            </p>
          ))}
          {result?.classify_job_id ? (
            <p className="text-xs text-muted-foreground" aria-live="polite">
              {classifying ? "New rows are being categorized in the background." : "New rows have been categorized."}
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        {outcome === null ? (
          <Button size="sm" onClick={start} disabled={running} aria-busy={running}>
            {running ? <Spinner /> : null}
            Start re-import
          </Button>
        ) : null}
        <Button size="sm" variant="ghost" onClick={onClose} disabled={running}>
          {outcome === null ? "Cancel" : "Close"}
        </Button>
      </div>
    </div>
  );
}
