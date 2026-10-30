import { useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { useUsers } from "@/api/hooks";
import { FileDropZone } from "@/components/primitives/file-drop-zone";
import { ErrorState, LoadingState } from "@/components/primitives/states";
import { useSelectedUser } from "@/hooks/use-selected-user";
import type { JobEvent } from "@/hooks/use-job-progress";

import { useExtractStatement, useImportSettings, useUploadStatement } from "./api";
import { IssuerExtractorPicker } from "./issuer-extractor-picker";
import { StatementCard } from "./statement-card";
import { PageSelector } from "./page-selector";
import { SummaryBar } from "./summary-bar";
import type { ExtractorChoice, ImportItem } from "./types";
import {
  extractorChoiceFromProposal,
  refreshAfterJob,
  shouldAutoConfirm,
  useImportQueue,
} from "./use-import-queue";

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
  const llmMode = settingsQuery.data?.llm.mode;
  const llmConfigured = llmMode !== undefined && llmMode !== "none";

  const users = usersQuery.data ?? [];
  // The upload target is the app-wide selected user. Only when "All users" is selected do we
  // ask which user to import for (the pick is local to this screen).
  const sel = useSelectedUser();
  const [pickedUserId, setPickedUserId] = React.useState<number | null>(null);
  const selectedUserId: number | null =
    sel.selectedUserId ?? (users.some((u) => u.id === pickedUserId) ? pickedUserId : null);

  const confirmImport = React.useCallback(
    (item: ImportItem) => {
      if (item.statementId === undefined || item.issuerId === null || item.extractorChoice === null) {
        return;
      }
      if (item.selectedPages?.length === 0) return;
      const choice = item.extractorChoice;
      extractStatement.mutate(
        {
          statementId: item.statementId,
          body: {
            issuer_id: item.issuerId,
            layout_spec_id: choice.kind === "spec" ? choice.layoutSpecId : null,
            parser_id: choice.kind === "parser" ? choice.parserId : null,
            remember: item.remember,
            pages: item.selectedPages ?? null,
          },
        },
        {
          onSuccess: (response) => {
            dispatch({ type: "extract_started", clientId: item.clientId, jobId: response.job_id });
          },
          onError: (error) => {
            dispatch({
              type: "extract_failed",
              clientId: item.clientId,
              message: error instanceof Error ? error.message : "Import failed",
            });
          },
        },
      );
    },
    [extractStatement, dispatch],
  );

  const uploadFile = React.useCallback(
    (clientId: string, file: File, userId: number) => {
      uploadStatement.mutate(
        { file, userId },
        {
          onSuccess: (response) => {
            dispatch({
              type: "upload_succeeded",
              clientId,
              statement: response.statement,
              proposal: response.proposal,
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
            if (
              response.statement.status === "awaiting_extractor" &&
              shouldAutoConfirm(updatedItem)
            ) {
              confirmImport(updatedItem);
            }
          },
          onError: (error) => {
            dispatch({
              type: "upload_failed",
              clientId,
              message: error instanceof Error ? error.message : "Upload failed",
            });
          },
        },
      );
    },
    [uploadStatement, dispatch, confirmImport, llmConfigured],
  );

  const handleFiles = React.useCallback(
    (files: File[]) => {
      if (selectedUserId === null) return;
      const clientIds = queueFiles(files, selectedUserId);
      files.forEach((file, index) => {
        const clientId = clientIds[index];
        if (clientId !== undefined) uploadFile(clientId, file, selectedUserId);
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
      );
    },
    [items, dispatch, queryClient, llmConfigured],
  );

  if (usersQuery.isLoading) return <LoadingState rows={3} />;
  if (usersQuery.isError) {
    return <ErrorState description="Could not load users." onRetry={() => void usersQuery.refetch()} />;
  }

  return (
    <div className="flex flex-col gap-6 py-2">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Import statements</h1>
        <p className="text-sm text-muted-foreground">
          Drop one or more statement PDFs. Each is matched to an issuer and extractor
          automatically; override either before importing.
        </p>
      </div>

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

      <FileDropZone onFiles={handleFiles} disabled={selectedUserId === null} />

      <SummaryBar items={items} />

      {items.length > 0 ? (
        <div className="flex flex-col gap-3">
          {items.map((item) => (
            <StatementCard
              key={item.clientId}
              item={item}
              onJobEvent={handleJobEvent}
              onConfirm={() => confirmImport(item)}
              onRememberChange={(remember) =>
                dispatch({ type: "remember_toggled", clientId: item.clientId, remember })
              }
              pages={<PageSelector item={item} dispatch={dispatch} />}
              picker={
                <IssuerExtractorPicker
                  item={item}
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
    </div>
  );
}

export default ImportScreen;

export const screenMeta = { path: "/spending/import", title: "Import" };
