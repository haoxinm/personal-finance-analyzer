import { useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { Link } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";

import {
  type WatchFile,
  type WatchStatus,
  useDismissAllWatchFiles,
  useDismissWatchFile,
  useRetryWatchFile,
  useUndismissWatchFile,
  useScanNow,
  useWatchFiles,
  useWatchStatus,
} from "./folder-imports-api";
import { statementTransactionsUrl } from "./statement-format";
import { isDismissedListOpen, setDismissedListOpen } from "./storage";
import { useSafeToast } from "./use-safe-toast";

function formatTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "unknown" : date.toLocaleString();
}

/** One sentence about the last scan: what it imported, what needs the user, what still waits. */
function lastScanSummary(status: WatchStatus): string {
  const last = status.last_scan;
  if (!last) return "No scan has run yet.";
  if (last.busy) return "The last scan was skipped because another scan was running.";
  if (last.error) return last.error;
  const parts = [`${last.imported} imported`];
  if (last.failed > 0) parts.push(`${last.failed} need${last.failed === 1 ? "s" : ""} attention`);
  if (last.waiting > 0) parts.push(`${last.waiting} waiting for the copy to finish`);
  return `${parts.join(", ")} (${last.scanned} PDF${last.scanned === 1 ? "" : "s"} in the folder).`;
}

const OUTCOME_LABEL: Record<WatchFile["outcome"], string> = {
  imported: "Imported",
  known: "Already in the app",
  failed: "Needs attention",
  ignored: "Not imported",
};

function resultLink(file: WatchFile): React.ReactNode {
  if (file.statement_id != null) {
    return (
      <Link className="underline underline-offset-4" to={statementTransactionsUrl(file.statement_id)}>
        View transactions
      </Link>
    );
  }
  if (file.tax_document_id != null) {
    return (
      <Link className="underline underline-offset-4" to="/tax/documents">
        View tax documents
      </Link>
    );
  }
  return null;
}

function RecentRow({ file }: { file: WatchFile }) {
  return (
    <li className="flex flex-col gap-1 rounded-md border p-3 text-sm" data-testid="folder-import-recent">
      <div className="flex flex-wrap items-center gap-2">
        <span className="break-all font-mono text-xs">{file.path}</span>
        <Badge variant={file.outcome === "imported" ? "default" : "secondary"}>{OUTCOME_LABEL[file.outcome]}</Badge>
        {file.kind ? <Badge variant="outline">{file.kind === "tax" ? "Tax form" : "Statement"}</Badge> : null}
      </div>
      {file.message ? <p className="text-xs text-muted-foreground">{file.message}</p> : null}
      {(file.warnings ?? []).map((warning) => (
        <p key={warning} className="text-xs text-amber-700 dark:text-amber-400">
          Note: {warning}
        </p>
      ))}
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        {file.folder_person ? <span>Person folder: {file.folder_person}</span> : null}
        <span>{formatTime(file.last_seen)}</span>
        {resultLink(file)}
      </div>
    </li>
  );
}

function ProblemRow({
  file,
  onRetry,
  onReimport,
  retrying,
  onDismiss,
  dismissing,
}: {
  file: WatchFile;
  onRetry: () => void;
  /** Open the statement's Re-import panel (a file read as 0 transactions cannot be retried). */
  onReimport?: (statementId: number) => void;
  retrying: boolean;
  onDismiss: () => void;
  dismissing: boolean;
}) {
  const retryLabel = file.code === "deleted_in_app" ? "Import again" : "Retry";
  const busy = retrying || dismissing;
  // Retry would do nothing for a statement that was read but empty (the server refuses it): the
  // fix is a re-import with another extractor or other pages, in the Imported statements panel.
  const reimportId = file.code === "no_transactions" && file.statement_id != null ? file.statement_id : null;
  return (
    <li
      className="flex flex-col gap-1 rounded-md border border-destructive/40 p-3 text-sm"
      data-testid="folder-import-problem"
      data-file-id={file.id}
      aria-busy={busy}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="min-w-0 break-all font-mono text-xs">{file.path}</span>
        <div className="flex shrink-0 items-center gap-1">
          {file.code === "no_transactions" ? (
            reimportId !== null && onReimport ? (
              <Button
                size="sm"
                variant="outline"
                onClick={() => onReimport(reimportId)}
                disabled={busy}
                aria-label={`Re-import ${file.path}`}
              >
                Re-import…
              </Button>
            ) : null
          ) : (
            <Button
              size="sm"
              variant="outline"
              onClick={onRetry}
              disabled={busy}
              aria-label={`${retryLabel} ${file.path}`}
            >
              {retrying ? <Spinner /> : null}
              {retryLabel}
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={onDismiss} disabled={busy} aria-label={`Dismiss ${file.path}`}>
            {dismissing ? <Spinner /> : null}
            Dismiss
          </Button>
        </div>
      </div>
      {file.message ? <p>{file.message}</p> : null}
      {file.hint ? <p className="text-xs text-muted-foreground">{file.hint}</p> : null}
    </li>
  );
}

function DismissedRow({
  file,
  onUndismiss,
  pending,
}: {
  file: WatchFile;
  onUndismiss: () => void;
  pending: boolean;
}) {
  return (
    <li className="flex flex-col gap-1 rounded-md border p-3 text-sm" data-testid="folder-import-dismissed">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="min-w-0 break-all font-mono text-xs">{file.path}</span>
        <Button
          size="sm"
          variant="ghost"
          onClick={onUndismiss}
          disabled={pending}
          aria-label={`Show again ${file.path}`}
        >
          {pending ? <Spinner /> : null}
          Show again
        </Button>
      </div>
      {file.message ? <p className="text-xs text-muted-foreground">{file.message}</p> : null}
    </li>
  );
}

/**
 * "Folder imports": PDFs put into the documents folder by hand (`<Person>/Statements/...`,
 * `<Person>/Tax/<year>/...`) are imported by a background scan. This shows whether it is on, when it
 * last ran and will run next, a "Scan now" button, what was imported, and what needs attention
 * with a Retry per file. Turning it on is a setting (Settings, Folder imports).
 */
export function FolderImportsPanel({ onReimportStatement }: { onReimportStatement?: (statementId: number) => void } = {}) {
  const queryClient = useQueryClient();
  const status = useWatchStatus();
  const recent = useWatchFiles("recent");
  const problems = useWatchFiles("problems");
  const dismissed = useWatchFiles("dismissed");
  const scan = useScanNow();
  const retry = useRetryWatchFile();
  const dismiss = useDismissWatchFile();
  const undismiss = useUndismissWatchFile();
  const dismissAll = useDismissAllWatchFiles();
  const toast = useSafeToast();
  const [confirmingDismissAll, setConfirmingDismissAll] = React.useState(false);
  const [pendingDismiss, setPendingDismiss] = React.useState<ReadonlySet<number>>(new Set());
  const [dismissedOpen, setDismissedOpen] = React.useState(isDismissedListOpen);
  const [showingAll, setShowingAll] = React.useState(false);
  const headingRef = React.useRef<HTMLHeadingElement>(null);
  const confirmRef = React.useRef<HTMLDivElement>(null);
  const dismissAllBtn = React.useRef<HTMLButtonElement>(null);
  const returnFocusToTrigger = React.useRef(false);
  // After a dismiss the row disappears when the list refreshes; focus then goes to the next row's
  // first button, or to the section heading when it was the last.
  const [focusAfter, setFocusAfter] = React.useState<{ gone: number; next: number | null } | null>(null);

  const markPending = (id: number, on: boolean) =>
    setPendingDismiss((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  const handleDismiss = (file: WatchFile, nextId: number | null) => {
    markPending(file.id, true);
    dismiss.mutateAsync(file.id).then(
      () => {
        markPending(file.id, false);
        setFocusAfter({ gone: file.id, next: nextId });
        toast({
          title: `Dismissed ${file.path}`,
          description: "It will show again if the file changes.",
          duration: 8000,
          action: { label: "Undo", onClick: () => undismiss.mutate(file.id) },
        });
      },
      () => markPending(file.id, false), // the hook's onError already toasted
    );
  };

  const closeConfirm = (restoreFocus: boolean) => {
    returnFocusToTrigger.current = restoreFocus;
    setConfirmingDismissAll(false);
  };

  const handleDismissAll = () => {
    closeConfirm(false);
    headingRef.current?.focus();
    dismissAll.mutateAsync().then(
      (count) => {
        toast({
          title: `Dismissed ${count} file${count === 1 ? "" : "s"}`,
          description: "They show again if they change. Open \u201cDismissed\u201d below to bring one back.",
        });
      },
      () => undefined,
    );
  };

  const handleShowAllAgain = () => {
    setShowingAll(true);
    void Promise.allSettled(dismissedItems.map((file) => undismiss.mutateAsync(file.id))).then(() => {
      setShowingAll(false);
      toast({ title: "Showing all dismissed files again", variant: "success" });
    });
  };

  const toggleDismissedOpen = (open: boolean) => {
    setDismissedOpen(open);
    setDismissedListOpen(open);
  };

  React.useEffect(() => {
    if (confirmingDismissAll) confirmRef.current?.focus();
    else if (returnFocusToTrigger.current) {
      returnFocusToTrigger.current = false;
      dismissAllBtn.current?.focus();
    }
  }, [confirmingDismissAll]);

  // When a scan that imported something finishes, everything that shows imported data is stale.
  const finishedAt = status.data?.last_scan?.finished_at ?? null;
  const importedCount = status.data?.last_scan?.imported ?? 0;
  const seenFinish = React.useRef<string | null | undefined>(undefined);
  React.useEffect(() => {
    if (seenFinish.current === undefined) {
      seenFinish.current = finishedAt;
      return;
    }
    if (finishedAt !== seenFinish.current) {
      seenFinish.current = finishedAt;
      void queryClient.invalidateQueries({ queryKey: ["watch"] });
      if (importedCount > 0) void queryClient.invalidateQueries();
    }
  }, [finishedAt, importedCount, queryClient]);

  const data = status.data;
  const running = data?.running === true || scan.isPending;
  const problemItems = React.useMemo(() => problems.data?.items ?? [], [problems.data]);
  const recentItems = recent.data?.items ?? [];
  const dismissedItems = dismissed.data?.items ?? [];

  React.useEffect(() => {
    if (focusAfter === null || problemItems.some((f) => f.id === focusAfter.gone)) return;
    const target =
      focusAfter.next !== null
        ? document.querySelector<HTMLElement>(`[data-file-id="${focusAfter.next}"] button[aria-label^="Dismiss "]:not([disabled])`)
        : null;
    (target ?? headingRef.current)?.focus();
    setFocusAfter(null);
  }, [focusAfter, problemItems]);

  return (
    <Card data-testid="folder-imports-panel" role="region" aria-labelledby="folder-imports-title">
      <CardHeader>
        <CardTitle id="folder-imports-title">Folder imports</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {status.isPending ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : status.isError || !data ? (
          <p role="alert" className="text-sm text-destructive">
            Could not load the folder-import status:{" "}
            {status.error instanceof Error ? status.error.message : "unknown error"}
          </p>
        ) : (
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-3">
              <Badge variant={data.enabled ? "default" : "secondary"} data-testid="folder-imports-state">
                {data.enabled ? "On" : "Off"}
              </Badge>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => scan.mutate()}
                disabled={running}
                aria-busy={running}
              >
                {running ? <Spinner /> : null}
                {running ? "Scanning…" : "Scan now"}
              </Button>
              {!data.enabled ? (
                <Link className="text-sm underline underline-offset-4" to="/settings#folder-imports">
                  Turn on in Settings
                </Link>
              ) : null}
            </div>
            <p className="text-xs text-muted-foreground">
              Drop PDFs into <span className="break-all font-mono">{data.documents_dir}</span> under{" "}
              <span className="font-mono">&lt;Person&gt;/Statements/</span> or{" "}
              <span className="font-mono">&lt;Person&gt;/Tax/&lt;year&gt;/</span>. The person folder must be the
              name of a person in the app.
            </p>
            <p className="text-xs text-muted-foreground" data-testid="folder-imports-last-scan">
              Last scan: {formatTime(data.last_scan?.finished_at ?? data.last_scan?.started_at)}.{" "}
              {lastScanSummary(data)}
              {data.enabled ? ` Next scan: ${formatTime(data.next_scan_at)}.` : ""}
            </p>
            {data.problem ? (
              <p role="alert" className="text-sm text-destructive">
                {data.problem}
              </p>
            ) : null}
          </div>
        )}

        <section aria-label="Files that need attention" className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 ref={headingRef} tabIndex={-1} className="text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring">
              Needs attention{problems.data ? ` (${problems.data.total})` : ""}
            </h3>
            {problemItems.length >= 2 && !confirmingDismissAll ? (
              <Button
                ref={dismissAllBtn}
                size="sm"
                variant="ghost"
                onClick={() => setConfirmingDismissAll(true)}
                disabled={dismissAll.isPending}
              >
                Dismiss all
              </Button>
            ) : null}
          </div>
          {confirmingDismissAll ? (
            <div
              ref={confirmRef}
              role="alertdialog"
              aria-label="Confirm dismiss all"
              tabIndex={-1}
              className="flex flex-wrap items-center gap-3 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  e.stopPropagation();
                  closeConfirm(true);
                }
              }}
            >
              <span>
                Dismiss {problems.data?.total ?? problemItems.length} files? They show again if they change.
              </span>
              <Button size="sm" onClick={handleDismissAll}>
                Dismiss all
              </Button>
              <Button size="sm" variant="ghost" onClick={() => closeConfirm(true)}>
                Cancel
              </Button>
            </div>
          ) : null}
          {problemItems.length === 0 ? (
            <p className="text-xs text-muted-foreground">Nothing needs attention.</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {problemItems.map((file, index) => (
                <ProblemRow
                  key={file.id}
                  file={file}
                  retrying={retry.isPending && retry.variables === file.id}
                  dismissing={pendingDismiss.has(file.id)}
                  onRetry={() => retry.mutate(file.id)}
                  onReimport={onReimportStatement}
                  onDismiss={() => handleDismiss(file, (problemItems[index + 1] ?? problemItems[index - 1])?.id ?? null)}
                />
              ))}
            </ul>
          )}
          {dismissedItems.length > 0 ? (
            <details
              className="rounded-md border border-dashed p-3"
              data-testid="folder-imports-dismissed"
              open={dismissedOpen}
              onToggle={(e) => {
                const open = e.currentTarget.open;
                if (open !== dismissedOpen) toggleDismissedOpen(open);
              }}
            >
              <summary className="cursor-pointer text-sm text-muted-foreground">
                Dismissed ({dismissed.data?.total ?? dismissedItems.length})
              </summary>
              <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs text-muted-foreground">
                  Hidden from &ldquo;Needs attention&rdquo; until the file changes.
                </p>
                <Button size="sm" variant="ghost" onClick={handleShowAllAgain} disabled={showingAll}>
                  {showingAll ? <Spinner /> : null}
                  Show all again
                </Button>
              </div>
              <ul className="mt-2 flex flex-col gap-2">
                {dismissedItems.map((file) => (
                  <DismissedRow
                    key={file.id}
                    file={file}
                    pending={undismiss.isPending && undismiss.variables === file.id}
                    onUndismiss={() => undismiss.mutate(file.id)}
                  />
                ))}
              </ul>
            </details>
          ) : null}
        </section>

        <section aria-label="Recent folder imports" className="flex flex-col gap-2">
          <h3 className="text-sm font-medium">Recent imports</h3>
          {recentItems.length === 0 ? (
            <p className="text-xs text-muted-foreground">Nothing has been imported from the folder yet.</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {recentItems.map((file) => (
                <RecentRow key={file.id} file={file} />
              ))}
            </ul>
          )}
        </section>
      </CardContent>
    </Card>
  );
}
