import { useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { useUsers } from "@/api/hooks";
import { FileDropZone } from "@/components/primitives/file-drop-zone";
import { Page, PageHeader } from "@/components/primitives/page";
import { ErrorState, LoadingState } from "@/components/primitives/states";
import { useSelectedUser } from "@/hooks/use-selected-user";
import type { JobEvent } from "@/hooks/use-job-progress";

import {
  fetchStatement,
  reproposeStatement,
  useExtractStatement,
  useImportSettings,
  useIssuers,
  useLayoutSpecs,
  useUploadStatement,
} from "./api";
import { FolderImportsPanel } from "./folder-imports-panel";
import { ImportedStatementsPanel, type OpenRequest } from "./imported-statements-panel";
import { IssuerExtractorPicker } from "./issuer-extractor-picker";
import { StatementCard } from "./statement-card";
import { PageSelector } from "./page-selector";
import { SummaryBar } from "./summary-bar";
import type { ExtractorChoice, ImportItem, Issuer, LayoutSpec } from "./types";
import {
  applyRefreshedProposal,
  extractorChoiceFromProposal,
  pendingPeers,
  refreshAfterJob,
  shouldAutoConfirm,
  useImportQueue,
} from "./use-import-queue";

/** A short name for an extractor choice, for the "remembered extractor" suggestion. */
function describeChoice(choice: ExtractorChoice | null, specs: readonly LayoutSpec[]): string {
  if (choice === null) return "an extractor";
  if (choice.kind === "spec") {
    const spec = specs.find((s) => s.id === choice.layoutSpecId);
    return spec ? `${spec.name} v${spec.version}` : "a saved layout";
  }
  return choice.parserId === "llm" ? "the LLM" : choice.parserId.replace(/_/g, " ");
}

/** The "remember" checkbox's wording for `item`: exactly which extractor is kept for which bank,
 * and, when the bank already remembers a different one, that this replaces it. */
function rememberInfoFor(
  item: ImportItem,
  issuers: readonly Issuer[],
  specs: readonly LayoutSpec[],
): { label: string; note?: string } | undefined {
  const issuer = issuers.find((i) => i.id === item.issuerId);
  if (!issuer || item.extractorChoice === null) return undefined;
  const chosen = describeChoice(item.extractorChoice, specs);
  const stored = extractorChoiceFromProposal({
    layout_spec_id: issuer.default_spec_id ?? null,
    parser_id: issuer.default_parser_id ?? null,
  });
  const info: { label: string; note?: string } = { label: `Use ${chosen} for future ${issuer.name} statements` };
  const same =
    stored !== null &&
    stored.kind === item.extractorChoice.kind &&
    (stored.kind === "spec"
      ? stored.layoutSpecId === (item.extractorChoice as typeof stored).layoutSpecId
      : stored.parserId === (item.extractorChoice as typeof stored).parserId);
  if (stored !== null && !same) {
    const was = describeChoice(stored, specs);
    info.note = item.remember
      ? `${issuer.name} currently remembers ${was}. Importing will replace it with ${chosen}; untick the box to keep ${was}.`
      : `${issuer.name} currently remembers ${was}; it will stay that way.`;
  }
  return info;
}

/** The Import screen (P3-A): drag-and-drop multiple statement PDFs, resolve the two-phase
 * import's user choice up front (D4) and its issuer/extractor choice per file (§2f.2), and
 * show live progress and outcomes through to reconciliation. */
function ImportScreen() {
  const usersQuery = useUsers();
  const queryClient = useQueryClient();
  const { items, queueFiles, dispatch } = useImportQueue();
  const uploadStatement = useUploadStatement();
  const extractStatement = useExtractStatement();
  const settingsQuery = useImportSettings();
  const issuersQuery = useIssuers();
  const specsQuery = useLayoutSpecs();
  const llmMode = settingsQuery.data?.llm.mode;
  const llmConfigured = llmMode !== undefined && llmMode !== "none";

  const users = usersQuery.data ?? [];
  // The upload target is the app-wide selected user. Only when "All users" is selected do we
  // ask which user to import for (the pick is local to this screen).
  const sel = useSelectedUser();
  const dropRef = React.useRef<HTMLDivElement>(null);
  const [finishHint, setFinishHint] = React.useState<string | null>(null);
  const [openRequest, setOpenRequest] = React.useState<OpenRequest | null>(null);
  const [pickedUserId, setPickedUserId] = React.useState<number | null>(null);
  const selectedUserId: number | null =
    sel.selectedUserId ?? (users.some((u) => u.id === pickedUserId) ? pickedUserId : null);

  // `mutateAsync`, not `mutate(vars, { onSuccess, onError })`: TanStack Query v5 fires per-call
  // callbacks only for the *latest* `mutate` on a shared `useMutation`, so with several files in
  // flight every earlier file would sit at "Uploading…" forever. Each promise settles for its own
  // call regardless of how many others are running.
  // Items whose extract request is in flight: a second confirm (a peer refresh racing the
  // user's click, or a double click) must never send another.
  const inFlight = React.useRef(new Set<string>());
  const confirmImport = React.useCallback(
    async (item: ImportItem) => {
      if (item.statementId === undefined || item.issuerId === null || item.extractorChoice === null) {
        return;
      }
      if (item.selectedPages?.length === 0) return;
      if (inFlight.current.has(item.clientId)) return;
      inFlight.current.add(item.clientId);
      const choice = item.extractorChoice;
      try {
        const response = await extractStatement.mutateAsync({
          statementId: item.statementId,
          body: {
            issuer_id: item.issuerId,
            layout_spec_id: choice.kind === "spec" ? choice.layoutSpecId : null,
            parser_id: choice.kind === "parser" ? choice.parserId : null,
            remember: item.remember,
            pages: item.selectedPages ?? null,
          },
        });
        dispatch({ type: "extract_started", clientId: item.clientId, jobId: response.job_id });
      } catch (error) {
        dispatch({
          type: "extract_failed",
          clientId: item.clientId,
          message: error instanceof Error ? error.message : "Import failed",
        });
      } finally {
        inFlight.current.delete(item.clientId);
      }
    },
    [extractStatement, dispatch],
  );

  // Pending files are proposed an issuer and extractor once, at upload. When something changes
  // what a fresh upload would be offered (an extractor remembered, an issuer created, "don't ask
  // again" ticked), the other pending files are asked again, one after the other. Files the user
  // has already decided about keep their choice and are only offered the new one.
  const itemsRef = React.useRef(items);
  itemsRef.current = items;
  const peerQueue = React.useRef<Promise<void>>(Promise.resolve());
  const autoStarted = React.useRef(new Set<string>());
  const refreshPeers = React.useCallback(
    (sourceClientId: string, issuerId: number | null) => {
      peerQueue.current = peerQueue.current
        .then(async () => {
          for (const peer of pendingPeers(itemsRef.current, sourceClientId, issuerId)) {
            // Never touch an item that is importing (or about to): an in-flight request, or one
            // the screen already started on its own.
            if (inFlight.current.has(peer.clientId) || autoStarted.current.has(peer.clientId)) continue;
            let response: Awaited<ReturnType<typeof reproposeStatement>>;
            try {
              response = await reproposeStatement(peer.statementId as number);
            } catch {
              // 409: already parsed (another tab, or it was started meanwhile), or its PDF is gone.
              // Quietly show the statement as it is now; never an error.
              if (!inFlight.current.has(peer.clientId) && peer.statementId !== undefined) {
                const current = await fetchStatement(peer.statementId).catch(() => null);
                const again = itemsRef.current.find((candidate) => candidate.clientId === peer.clientId);
                if (current?.status === "parsed" && again?.status === "awaiting_extractor" && !inFlight.current.has(peer.clientId)) {
                  dispatch({ type: "statement_refreshed", clientId: peer.clientId, statement: current, llmConfigured });
                }
              }
              continue;
            }
            const latest = itemsRef.current.find((candidate) => candidate.clientId === peer.clientId);
            if (!latest || latest.status !== "awaiting_extractor") continue;
            if (inFlight.current.has(latest.clientId) || autoStarted.current.has(latest.clientId)) continue;
            dispatch({
              type: "proposal_refreshed",
              clientId: latest.clientId,
              statement: response.statement,
              proposal: response.proposal,
            });
            const updated = applyRefreshedProposal(latest, response.statement, response.proposal);
            // Exactly what a fresh upload gets: one click saved only where it would be.
            if (
              !updated.choiceTouched &&
              response.statement.status === "awaiting_extractor" &&
              shouldAutoConfirm(updated) &&
              !autoStarted.current.has(updated.clientId)
            ) {
              autoStarted.current.add(updated.clientId);
              await confirmImport(updated);
            }
          }
        })
        .catch(() => undefined);
    },
    [dispatch, confirmImport, llmConfigured],
  );

  const uploadFile = React.useCallback(
    async (clientId: string, file: File, userId: number) => {
      let response: Awaited<ReturnType<typeof uploadStatement.mutateAsync>>;
      try {
        response = await uploadStatement.mutateAsync({ file, userId });
      } catch (error) {
        dispatch({
          type: "upload_failed",
          clientId,
          message: error instanceof Error ? error.message : "Upload failed",
        });
        return;
      }
      dispatch({
        type: "upload_succeeded",
        clientId,
        statement: response.statement,
        proposal: response.proposal,
        duplicate: response.duplicate,
        llmConfigured,
      });
      const updatedItem: ImportItem = {
        clientId,
        fileName: file.name,
        userId,
        status: "awaiting_extractor",
        statementId: response.statement.id,
        statement: response.statement,
        proposal: response.proposal,
        issuerId: response.proposal.issuer_id,
        extractorChoice: extractorChoiceFromProposal(response.proposal),
        remember: true,
        selectedPages: response.statement.selected_pages ?? null,
        // A re-uploaded statement whose built-in parse already failed must not auto-run it.
        failure: response.statement.error_detail
          ? { detail: response.statement.error_detail, llm: false, previous: true }
          : undefined,
      };
      if (response.statement.status === "awaiting_extractor" && shouldAutoConfirm(updatedItem)) {
        autoStarted.current.add(clientId);
        await confirmImport(updatedItem);
      }
    },
    [uploadStatement, dispatch, confirmImport, llmConfigured],
  );

  const handleFiles = React.useCallback(
    (files: File[]) => {
      if (selectedUserId === null) return;
      const clientIds = queueFiles(files, selectedUserId);
      files.forEach((file, index) => {
        const clientId = clientIds[index];
        if (clientId !== undefined) void uploadFile(clientId, file, selectedUserId);
      });
    },
    [selectedUserId, queueFiles, uploadFile],
  );

  // Once an item's extract job reaches a terminal state, refresh its statement (and, once
  // parsed, its needs-review count) exactly once per terminal event.
  const refreshedJobIds = React.useRef(new Set<string>());
  const handleJobEvent = React.useCallback(
    (clientId: string, event: JobEvent) => {
      dispatch({ type: "job_event", clientId, event });
      if (event.status !== "done" && event.status !== "error") return;
      const item = items.find((candidate) => candidate.clientId === clientId);
      if (!item || item.statementId === undefined || item.jobId === undefined) return;
      const dedupeKey = `${clientId}:${item.jobId}`;
      if (refreshedJobIds.current.has(dedupeKey)) return;
      refreshedJobIds.current.add(dedupeKey);
      void refreshAfterJob(
        clientId,
        item.statementId,
        item.jobId,
        dispatch,
        queryClient,
        llmConfigured,
        (job) => {
          // The extractor was remembered by this import: the others from this issuer should use it.
          if (item.remember && job?.remembered) refreshPeers(clientId, item.issuerId);
        },
      );
    },
    [items, dispatch, queryClient, llmConfigured, refreshPeers],
  );

  // A statement awaiting an extractor (or whose import failed) is finished by dropping its PDF
  // again: the upload recognizes it and offers the extractor choice.
  const finishImport = React.useCallback((statement: { original_name: string; id: number }) => {
    setFinishHint(
      `Drop \u201c${statement.original_name || `statement ${statement.id}`}\u201d here again to choose an extractor and finish importing it.`,
    );
    const zone = dropRef.current;
    zone?.scrollIntoView?.({ block: "center", behavior: "smooth" });
    zone?.querySelector<HTMLElement>('[role="button"]')?.focus();
  }, []);
  const nonceRef = React.useRef(0);
  const reimportExisting = React.useCallback((statementId: number) => {
    nonceRef.current += 1;
    setOpenRequest({ statementId, nonce: nonceRef.current });
  }, []);
  const requestHandled = React.useCallback((nonce: number) => {
    setOpenRequest((prev) => (prev?.nonce === nonce ? null : prev));
  }, []);

  const header = (
    <PageHeader
      title="Import statements"
      description="Drop one or more statement PDFs. Each is matched to an issuer and extractor automatically; override either before importing."
    />
  );

  if (usersQuery.isLoading) {
    return (
      <Page>
        {header}
        <LoadingState rows={3} />
      </Page>
    );
  }
  if (usersQuery.isError) {
    return (
      <Page>
        {header}
        <ErrorState description="Could not load users." onRetry={() => void usersQuery.refetch()} />
      </Page>
    );
  }

  return (
    <Page>
      {header}

      {sel.selectedUserId === null ? (
        <label className="flex max-w-xs flex-col gap-1 text-xs font-medium text-muted-foreground">
          Import for which user? (&ldquo;All users&rdquo; is selected above)
          <select
            aria-label="Importing for"
            className="h-9 rounded-md border border-input bg-transparent px-3 text-sm text-foreground"
            value={selectedUserId ?? ""}
            onChange={(event) => setPickedUserId(event.target.value === "" ? null : Number(event.target.value))}
          >
            <option value="">Choose a user…</option>
            {users.map((user) => (
              <option key={user.id} value={user.id}>
                {user.name}
                {user.is_default ? " (default)" : ""}
              </option>
            ))}
          </select>
        </label>
      ) : (
        <p className="text-sm text-muted-foreground" data-testid="import-target">
          Importing for <strong className="text-foreground">{sel.selectedUser?.name}</strong>. Change the user above to import for someone else.
        </p>
      )}

      <div ref={dropRef} className="flex flex-col gap-2">
        <FileDropZone onFiles={handleFiles} disabled={selectedUserId === null} />
        {finishHint ? (
          <p role="status" className="text-sm text-muted-foreground" data-testid="finish-hint">
            {finishHint}
          </p>
        ) : null}
      </div>

      <SummaryBar items={items} />

      {items.length > 0 ? (
        <div className="flex flex-col gap-3">
          {items.map((item) => (
            <StatementCard
              key={item.clientId}
              item={item}
              onJobEvent={handleJobEvent}
              onConfirm={() => void confirmImport(item)}
              onReimportExisting={reimportExisting}
              onSkipConfirmChange={() => refreshPeers(item.clientId, item.issuerId)}
              suggestion={
                item.suggestedChoice
                  ? {
                      text: `${
                        (issuersQuery.data ?? []).find((i) => i.id === item.suggestedChoice?.issuerId)?.name ?? "This issuer"
                      }'s remembered extractor is ${describeChoice(item.suggestedChoice.extractorChoice, specsQuery.data ?? [])}.`,
                      onUse: () => dispatch({ type: "suggestion_applied", clientId: item.clientId }),
                    }
                  : undefined
              }
              rememberInfo={rememberInfoFor(item, issuersQuery.data ?? [], specsQuery.data ?? [])}
              onRememberChange={(remember) =>
                dispatch({ type: "remember_toggled", clientId: item.clientId, remember })
              }
              pages={<PageSelector item={item} dispatch={dispatch} />}
              picker={
                <IssuerExtractorPicker
                  item={item}
                  onIssuerCreated={(issuerId) => refreshPeers(item.clientId, issuerId)}
                  onIssuerChange={(issuerId: number, extractorChoice: ExtractorChoice | null) =>
                    dispatch({
                      type: "issuer_selected",
                      clientId: item.clientId,
                      issuerId,
                      extractorChoice,
                    })
                  }
                  onExtractorChange={(extractorChoice: ExtractorChoice) =>
                    dispatch({ type: "extractor_selected", clientId: item.clientId, extractorChoice })
                  }
                />
              }
            />
          ))}
        </div>
      ) : null}

      <ImportedStatementsPanel
        userId={sel.selectedUserId}
        onFinishImport={finishImport}
        request={openRequest}
        onRequestHandled={requestHandled}
      />

      <FolderImportsPanel onReimportStatement={reimportExisting} />
    </Page>
  );
}

export default ImportScreen;

export const screenMeta = { path: "/spending/import", title: "Import" };
