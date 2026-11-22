import { useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { Link } from "react-router-dom";

import type { components } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useJobProgress } from "@/hooks/use-job-progress";

import { fetchJob, isPdfNotFound } from "./api";
import {
  useExtractorCandidates,
  useReparseStatement,
  type ExtractorCandidate,
  type ReparseRequest,
} from "./imported-statements-api";
import { formatPageRanges, parsePageRanges } from "./page-ranges";
import {
  PDF_MISSING_REASON,
  describeReimport,
  extractorDisplayName,
  hasNoTransactions,
  plural,
} from "./statement-format";
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

/** The select value of a scored candidate. */
function candidateKey(c: ExtractorCandidate): string {
  if (c.kind === "spec") return `spec:${c.layout_spec_id}`;
  if (c.kind === "llm") return "llm";
  return `parser:${c.parser_id}`;
}

function candidateLabel(c: ExtractorCandidate): string {
  const parts = [c.score != null ? `${c.label} (${Math.round(c.score * 100)}% match)` : c.label];
  if (c.recorded) parts.push("recorded");
  if (c.remembered) parts.push("remembered");
  return parts.join(" · ");
}

/** What the picker preselects: the recorded extractor, except when it read nothing: then the best
 * scoring other one (never the LLM, which sends text out and needs a deliberate click). */
function defaultCandidate(candidates: readonly ExtractorCandidate[], noRows: boolean): ExtractorCandidate | undefined {
  const recorded = candidates.find((c) => c.recorded) ?? candidates[0];
  if (!noRows) return recorded;
  const better = candidates
    .filter((c) => !c.recorded && c.kind !== "llm" && (c.score ?? 0) > 0)
    .sort((a, b) => (b.score ?? 0) - (a.score ?? 0))[0];
  return better ?? recorded;
}

/**
 * The inline "Re-import" panel under a statement row: rebuild the statement from its stored PDF,
 * with any extractor that could read it (scored on the saved pages) and any pages. Hand edits are
 * kept unless the user opts to discard them; the job's result says how many came back, how many
 * could not be matched and how many were discarded. A re-import that would leave the statement
 * with no transactions at all is refused by the server and only goes ahead on an explicit second
 * confirmation. Whatever happens to the job the panel ends in a state the user can leave or retry
 * from; following the job and refreshing the screen is `followInBackground`'s work, so closing the
 * panel at any moment loses nothing.
 */
export function ReimportPanel({ statement, specs, llmConfigured, onClose }: ReimportPanelProps) {
  const queryClient = useQueryClient();
  const toast = useSafeToast();
  const reparse = useReparseStatement();
  const candidatesQuery = useExtractorCandidates(statement.id);
  const [choice, setChoice] = React.useState<string | null>(null);
  const [discard, setDiscard] = React.useState(false);
  const initialPages = React.useMemo<number[] | null>(() => {
    if (statement.selected_pages) return statement.selected_pages;
    return statement.page_count ? Array.from({ length: statement.page_count }, (_, i) => i + 1) : null;
  }, [statement.selected_pages, statement.page_count]);
  const initialPageText = initialPages ? formatPageRanges(initialPages) : "";
  const [pageText, setPageText] = React.useState(initialPageText);
  const [jobId, setJobId] = React.useState<string | null>(null);
  const [submitError, setSubmitError] = React.useState<string | null>(null);
  const [result, setResult] = React.useState<Job | null>(null);
  const [failure, setFailure] = React.useState<{ detail: string; code: string | null } | null>(null);
  const [classifyJobId, setClassifyJobId] = React.useState<string | null>(null);
  const { event } = useJobProgress(jobId);
  const { event: classifyEvent } = useJobProgress(classifyJobId);
  const headingRef = React.useRef<HTMLHeadingElement>(null);
  const confirmRef = React.useRef<HTMLDivElement>(null);

  const titleId = `reimport-title-${statement.id}`;
  const candidates = candidatesQuery.data;
  const issuerSpecs = specs.filter((s) => s.approved && s.issuer_id === statement.issuer_id);
  const recordedSpec = statement.layout_spec_id != null ? specs.find((s) => s.id === statement.layout_spec_id) : undefined;
  const recordedIsLlm = statement.parser_id === "llm";
  const sameLabel = recordedSpec
    ? `${recordedSpec.name} (v${recordedSpec.version})`
    : extractorDisplayName(statement.parser_id);
  const showLlm = llmConfigured && !recordedIsLlm;
  const noRows = hasNoTransactions(statement);

  const defaultKey = candidates && candidates.length > 0 ? candidateKey(defaultCandidate(candidates, noRows) as ExtractorCandidate) : "same";
  const selectedKey = choice ?? defaultKey;
  const selected = candidates?.find((c) => candidateKey(c) === selectedKey);

  const jobEnded = event?.status === "done" || event?.status === "error";
  const running = reparse.isPending || (jobId !== null && !jobEnded);
  const jobFailed = event?.status === "error";
  const classifying = classifyJobId !== null && classifyEvent?.status !== "done" && classifyEvent?.status !== "error";
  const usesLlm =
    selected !== undefined
      ? selected.kind === "llm"
      : selectedKey === "llm" || (selectedKey === "same" && recordedIsLlm && recordedSpec === undefined);

  // Pages: the saved selection, editable. Sent only when changed.
  const trimmedPages = pageText.trim();
  const parsedPages = trimmedPages === "" ? null : parsePageRanges(trimmedPages, statement.page_count ?? Number.MAX_SAFE_INTEGER);
  const pagesInvalid = trimmedPages === "" ? initialPages !== null : parsedPages === null || parsedPages.length === 0;
  const pagesChanged =
    parsedPages !== null && formatPageRanges(parsedPages) !== (initialPages ? formatPageRanges(initialPages) : "");

  React.useEffect(() => {
    headingRef.current?.focus();
  }, []);

  // The re-import job ended: always refresh; on success read its result once (never get stuck if
  // that read fails) and show the classification job it spawned; on failure read the error code
  // (an empty result asks for a confirmation instead of showing an error).
  const finishedFor = React.useRef<string | null>(null);
  React.useEffect(() => {
    if (jobId === null || !jobEnded || finishedFor.current === jobId) return;
    finishedFor.current = jobId;
    void queryClient.invalidateQueries();
    const unreadable =
      "The re-import finished, but its result could not be read. Your transactions were updated; check them before importing again.";
    if (event?.status !== "done") {
      const fallback = event?.error_detail ?? "The re-import failed.";
      void fetchJob(jobId)
        .then((job) => setFailure({ detail: job?.error_detail ?? fallback, code: job?.error_code ?? null }))
        .catch(() => setFailure({ detail: fallback, code: null }));
      return;
    }
    void fetchJob(jobId)
      .then((job) => {
        if (job === null) {
          setSubmitError(unreadable);
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
      .catch(() => setSubmitError(unreadable));
  }, [jobId, jobEnded, event?.status, event?.error_detail, queryClient, toast]);

  // The classification job ended: the new rows now have categories.
  const classifiedFor = React.useRef<string | null>(null);
  React.useEffect(() => {
    if (classifyJobId === null || classifyEvent === null) return;
    if (classifyEvent.status !== "done" && classifyEvent.status !== "error") return;
    if (classifiedFor.current === classifyJobId) return;
    classifiedFor.current = classifyJobId;
    void queryClient.invalidateQueries();
  }, [classifyJobId, classifyEvent, queryClient]);

  const emptyResult = failure?.code === "empty_result";
  React.useEffect(() => {
    if (emptyResult) confirmRef.current?.focus();
  }, [emptyResult]);

  const start = (allowEmpty = false) => {
    const body: ReparseRequest = { discard_edits: discard, remember: false, allow_empty: allowEmpty };
    if (candidates && candidates.length > 0) {
      if (selected && !selected.recorded) {
        if (selected.kind === "spec") body.layout_spec_id = selected.layout_spec_id ?? null;
        else body.parser_id = selected.parser_id ?? (selected.kind === "llm" ? "llm" : null);
      }
    } else if (selectedKey === "llm") body.parser_id = "llm";
    else if (selectedKey.startsWith("spec:")) body.layout_spec_id = Number(selectedKey.slice(5));
    if (pagesChanged && parsedPages) body.pages = parsedPages;
    setSubmitError(null);
    setFailure(null);
    setResult(null);
    setJobId(null);
    setClassifyJobId(null);
    reparse.mutateAsync({ statementId: statement.id, body }).then(
      (id) => setJobId(id),
      (error: unknown) => setSubmitError(isPdfNotFound(error) ? PDF_MISSING_REASON : errorMessage(error)),
    );
  };

  const keepTransactions = () => {
    setFailure(null);
    setJobId(null);
  };

  const outcome = result ? describeReimport(result) : null;
  const progress = Math.round((event?.progress ?? 0) * 100);
  const keptCount = statement.txn_count ?? 0;
  const pagesErrorId = `reimport-pages-error-${statement.id}`;
  const showForm = outcome === null && !emptyResult;

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

      {showForm ? (
        <>
          <label className="flex max-w-md flex-col gap-1 text-xs font-medium text-muted-foreground">
            Extractor
            <select
              className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground"
              value={selectedKey}
              onChange={(e) => setChoice(e.target.value)}
              disabled={running || candidatesQuery.isPending}
              aria-busy={candidatesQuery.isPending}
            >
              {candidates && candidates.length > 0 ? (
                candidates.map((c) => (
                  <option key={candidateKey(c)} value={candidateKey(c)}>
                    {candidateLabel(c)}
                  </option>
                ))
              ) : (
                <>
                  <option value="same">Same as before ({sameLabel})</option>
                  {issuerSpecs
                    .filter((s) => s.id !== statement.layout_spec_id)
                    .map((s) => (
                      <option key={s.id} value={`spec:${s.id}`}>
                        {s.name} (v{s.version})
                      </option>
                    ))}
                  {showLlm ? <option value="llm">LLM extractor</option> : null}
                </>
              )}
            </select>
          </label>
          {noRows ? (
            <p className="text-xs text-amber-700 dark:text-amber-400">
              The recorded extractor found no transactions here, so the best other match is preselected.
            </p>
          ) : null}
          {usesLlm ? (
            <p className="text-xs text-amber-700 dark:text-amber-400">
              Sends this statement&rsquo;s page text (after redaction) to the configured language model.
            </p>
          ) : null}
          <Link
            to={`/spending/layout-mapper?statement_id=${statement.id}`}
            className="self-start text-xs underline underline-offset-4"
          >
            Map a new layout
          </Link>

          <label className="flex max-w-xs flex-col gap-1 text-xs font-medium text-muted-foreground">
            Pages{statement.page_count ? ` (of ${statement.page_count})` : ""}
            <input
              className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground"
              value={pageText}
              onChange={(e) => setPageText(e.target.value)}
              placeholder="e.g. 1-3,5"
              disabled={running}
              aria-invalid={pagesInvalid || undefined}
              aria-describedby={pagesInvalid ? pagesErrorId : undefined}
            />
          </label>
          {pagesInvalid ? (
            <p id={pagesErrorId} role="alert" className="-mt-2 text-xs text-destructive">
              Enter page numbers like 1-3,5
              {statement.page_count ? ` (between 1 and ${statement.page_count})` : ""}.
            </p>
          ) : null}

          {keptCount > 0 ? (
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
          ) : null}
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
      {jobFailed && failure !== null && !emptyResult ? (
        <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-destructive">
          {failure.detail}
        </p>
      ) : null}

      {emptyResult && !running ? (
        <div
          ref={confirmRef}
          role="alertdialog"
          aria-label="Confirm re-import with no transactions"
          data-testid="empty-result-confirm"
          tabIndex={-1}
          className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <p className="font-medium">
            The re-import found no transactions. Your {plural(keptCount, "transaction")}{" "}
            {keptCount === 1 ? "is" : "are"} kept.
          </p>
          <p>Replace {keptCount === 1 ? "it" : "them"} with nothing anyway?</p>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={keepTransactions}>
              Keep my transactions
            </Button>
            <Button size="sm" variant="destructive" onClick={() => start(true)}>
              Re-import anyway (removes {plural(keptCount, "transaction")})
            </Button>
          </div>
        </div>
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
        {showForm ? (
          <Button size="sm" onClick={() => start()} disabled={running || pagesInvalid} aria-busy={running}>
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
