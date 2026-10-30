import * as React from "react";

import type { components } from "@/api/client";
import { UserSwitcher } from "@/components/primitives/user-switcher";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FileDropZone } from "@/components/primitives/file-drop-zone";

import { useToast } from "@/components/ui/use-toast";

import {
  useCreateManualDocument,
  useDeleteTaxDocument,
  useLlmReadAvailable,
  useReassignTaxDocument,
  useUploadTaxDocument,
  type TaxImportResponse,
} from "../hooks";
import {
  TAX_PRIVACY_NOTE,
  documentStatusLabel,
  errorMessage,
  formTypeLabel,
  skippedFromMessages,
  type TaxDocument,
} from "../logic";
import { DocumentDetail } from "./document-detail";
import { LlmReadSection } from "./llm-read-section";

type User = components["schemas"]["User"];

export interface DocumentsPanelProps {
  documents: readonly TaxDocument[];
  users: readonly User[];
  /** Selected user, or null for "All users". */
  selectedUserId: number | null;
}

interface UploadOutcome {
  name: string;
  status: TaxImportResponse["status"] | "failed";
  message: string;
  /** Extra detail lines: forms skipped as already imported, parser warnings. */
  details: string[];
  /** Set when a document row exists that figures can be added to by hand. */
  documentId?: number;
}

function outcomeFor(file: File, r: TaxImportResponse): UploadOutcome {
  const skipped = skippedFromMessages(r.skipped_from ?? []);
  if (skipped.length === 0 && r.skipped_duplicate_forms > 0) {
    skipped.push(
      `${r.skipped_duplicate_forms} duplicate form${r.skipped_duplicate_forms === 1 ? " was" : "s were"} skipped.`,
    );
  }
  const details = [...skipped, ...r.warnings];
  switch (r.status) {
    case "parsed":
      return {
        name: file.name,
        status: r.status,
        message: `Imported ${r.inserted_forms} form${r.inserted_forms === 1 ? "" : "s"}.`,
        details,
      };
    case "duplicate":
      return {
        name: file.name,
        status: r.status,
        message: "Already imported; nothing changed.",
        details,
      };
    default:
      return {
        name: file.name,
        status: r.status,
        message: r.error ?? "This file could not be read as a tax form.",
        details,
        documentId: r.document.id,
      };
  }
}

function DocumentRow({
  d: rowDoc,
  users,
  request,
}: {
  d: TaxDocument;
  users: readonly User[];
  /** A request (from the upload outcome or manual creation) to open this row's add-form editor. */
  request: { id: number; n: number } | null;
}) {
  // The owner the server confirmed after a move; the documents list catches up on refetch. Until
  // then every id-addressed call must already use the new owner (the old one would 404).
  const [movedTo, setMovedTo] = React.useState<number | null>(null);
  const doc =
    movedTo !== null && movedTo !== rowDoc.user_id
      ? { ...rowDoc, user_id: movedTo }
      : rowDoc;
  const d = doc;
  const remove = useDeleteTaxDocument(d.user_id);
  const reassign = useReassignTaxDocument(rowDoc.user_id);
  const [moveTo, setMoveTo] = React.useState<number | null>(null);
  const { toast } = useToast();
  const [open, setOpen] = React.useState(false);
  const [adding, setAdding] = React.useState(false);
  const [confirming, setConfirming] = React.useState(false);
  // Unique per rendered row: compare mode renders the same document in two panes.
  const uid = React.useId();
  const detailId = `tax-doc-detail-${d.id}-${uid}`;
  const llmId = `tax-doc-llm-${d.id}-${uid}`;
  const [llmOpen, setLlmOpen] = React.useState(false);
  const canReadWithLlm = useLlmReadAvailable(d);
  const requestN = request?.id === d.id ? request.n : 0;

  React.useEffect(() => {
    if (requestN > 0) {
      setOpen(true);
      setAdding(true);
    }
  }, [requestN]);

  async function onDelete() {
    try {
      await remove.mutateAsync(d.id);
    } catch (e) {
      toast({
        variant: "destructive",
        title: "Could not delete document",
        description: errorMessage(e),
      });
    }
  }

  async function onMove() {
    if (moveTo === null) return;
    const target = userName(users, moveTo);
    try {
      const moved = await reassign.mutateAsync({
        documentId: d.id,
        userId: moveTo,
      });
      setMovedTo(moved.user_id);
      // Open editors were built for the previous owner; close them (they reopen with the new one).
      setOpen(false);
      setAdding(false);
      setLlmOpen(false);
      toast({
        variant: "success",
        title: `Moved to ${target}`,
        description: d.original_name,
      });
    } catch (e) {
      toast({
        variant: "destructive",
        title: "Could not move document",
        description: errorMessage(e),
      });
    } finally {
      setMoveTo(null);
    }
  }

  const failed = d.status !== "parsed";
  const otherUsers = users.filter((u) => u.id !== d.user_id);
  return (
    <li className="flex flex-col gap-2 p-3" data-testid="tax-document">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">{d.original_name}</span>
        {d.form_types.map((f) => (
          <Badge key={f} variant="secondary">
            {formTypeLabel(f)}
          </Badge>
        ))}
        <Badge variant={d.status === "parsed" ? "accent" : "destructive"}>
          {documentStatusLabel(d.status)}
        </Badge>
        <Badge variant="outline" data-testid="tax-document-owner" title="Owner">
          {userName(users, d.user_id)}
        </Badge>
        <span className="text-xs text-muted-foreground">
          {d.tax_year ?? "year unknown"}
        </span>
        <span className="ml-auto flex flex-wrap items-center gap-2">
          {users.length > 1 && moveTo === null ? (
            <select
              aria-label={`Move ${d.original_name} to another user`}
              className="h-8 rounded-md border border-input bg-background px-2 text-xs"
              value=""
              onChange={(e) =>
                setMoveTo(e.target.value === "" ? null : Number(e.target.value))
              }
            >
              <option value="">Move to…</option>
              {otherUsers.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.name}
                </option>
              ))}
            </select>
          ) : null}
          {moveTo !== null ? (
            <>
              <span className="text-xs">
                Move this document and its figures to {userName(users, moveTo)}?
              </span>
              <Button
                size="sm"
                disabled={reassign.isPending}
                onClick={() => void onMove()}
              >
                Confirm move
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setMoveTo(null)}
              >
                Cancel move
              </Button>
            </>
          ) : null}
          {failed ? (
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setOpen(true);
                setAdding(true);
              }}
            >
              Add figures manually
            </Button>
          ) : null}
          {canReadWithLlm ? (
            <Button
              size="sm"
              variant="outline"
              aria-expanded={llmOpen}
              aria-controls={llmId}
              aria-label={`Read pages of ${d.original_name} with LLM`}
              onClick={() => setLlmOpen((v) => !v)}
            >
              Read pages with LLM…
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="outline"
            aria-expanded={open}
            aria-controls={detailId}
            aria-label={`Review figures for ${d.original_name}`}
            onClick={() => setOpen((v) => !v)}
          >
            Review figures
          </Button>
          {confirming ? (
            <>
              <span className="text-xs">
                Delete this document and its figures?
              </span>
              <Button
                size="sm"
                variant="destructive"
                disabled={remove.isPending}
                onClick={() => void onDelete()}
              >
                Confirm delete
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setConfirming(false)}
              >
                Cancel
              </Button>
            </>
          ) : (
            <Button
              size="sm"
              variant="outline"
              aria-label={`Delete ${d.original_name}`}
              onClick={() => setConfirming(true)}
            >
              Delete
            </Button>
          )}
        </span>
      </div>
      {d.error_detail ? (
        <p className="text-xs text-destructive">{d.error_detail}</p>
      ) : null}
      {d.warnings.length > 0 ? (
        <ul className="list-disc pl-5 text-xs text-muted-foreground">
          {d.warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      ) : null}
      {canReadWithLlm && llmOpen ? (
        <div id={llmId}>
          <LlmReadSection document={d} />
        </div>
      ) : null}
      {open ? (
        <div id={detailId} className="rounded-md bg-muted/30 p-2">
          <DocumentDetail
            document={d}
            adding={adding}
            onAddingChange={setAdding}
          />
        </div>
      ) : null}
    </li>
  );
}

function userName(users: readonly User[], id: number): string {
  return users.find((u) => u.id === id)?.name ?? `User ${id}`;
}

export function DocumentsPanel({
  documents,
  users,
  selectedUserId,
}: DocumentsPanelProps) {
  const upload = useUploadTaxDocument();
  const createManual = useCreateManualDocument();
  const { toast } = useToast();
  // The drop zone needs one concrete user; with "All users" selected the user picks one here.
  const [uploadUser, setUploadUser] = React.useState<number | null>(null);
  const targetUserId = selectedUserId ?? uploadUser ?? users[0]?.id ?? null;
  const [taxYear, setTaxYear] = React.useState("");
  const [outcomes, setOutcomes] = React.useState<UploadOutcome[]>([]);
  const [uploading, setUploading] = React.useState(false);
  const [request, setRequest] = React.useState<{
    id: number;
    n: number;
  } | null>(null);

  const yearNumber = taxYear.trim() === "" ? undefined : Number(taxYear);
  const yearInvalid =
    yearNumber !== undefined &&
    (!Number.isInteger(yearNumber) || yearNumber < 1990 || yearNumber > 2100);

  function openAdd(id: number) {
    setRequest((r) => ({ id, n: (r?.n ?? 0) + 1 }));
  }

  async function onManual() {
    if (targetUserId === null) return;
    if (yearNumber === undefined || yearInvalid) {
      toast({
        variant: "destructive",
        title: "Enter the tax year first",
        description: "Fill in the tax year field above.",
      });
      return;
    }
    try {
      const doc = await createManual.mutateAsync({
        user_id: targetUserId,
        tax_year: yearNumber,
      });
      openAdd(doc.id);
    } catch (e) {
      toast({
        variant: "destructive",
        title: "Could not create document",
        description: errorMessage(e),
      });
    }
  }

  async function onFiles(files: File[]) {
    if (targetUserId === null || yearInvalid) return;
    setUploading(true);
    const results: UploadOutcome[] = [];
    for (const file of files) {
      try {
        const r = await upload.mutateAsync({
          file,
          userId: targetUserId,
          taxYear: yearNumber,
        });
        results.push(outcomeFor(file, r));
      } catch (e) {
        results.push({
          name: file.name,
          status: "failed",
          message: errorMessage(e, "Upload failed."),
          details: [],
        });
      }
    }
    setOutcomes(results);
    setUploading(false);
  }

  return (
    <div className="flex flex-col gap-4" data-testid="tax-documents">
      <div className="flex flex-col gap-2">
        {selectedUserId === null && users.length > 1 ? (
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted-foreground">Upload for</span>
            <UserSwitcher
              users={users}
              value={targetUserId}
              onChange={setUploadUser}
              allowAll={false}
              variant="select"
              aria-label="Upload for user"
            />
          </label>
        ) : null}
        <FileDropZone
          onFiles={(f) => void onFiles(f)}
          disabled={uploading || targetUserId === null || yearInvalid}
          title="Drag tax forms here, or click to browse"
          hint="W-2, 1099 and 1040 PDFs with a text layer. Scanned forms can’t be read."
          inputLabel="Choose tax form PDFs"
        />
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          Tax year (only if a form doesn’t print one)
          <Input
            type="number"
            inputMode="numeric"
            min={1990}
            max={2100}
            value={taxYear}
            onChange={(e) => setTaxYear(e.target.value)}
            aria-invalid={yearInvalid}
            className="h-8 w-24"
          />
        </label>
        <p
          className="text-xs text-muted-foreground"
          data-testid="tax-privacy-note"
        >
          {TAX_PRIVACY_NOTE}
        </p>
        <div>
          <Button
            size="sm"
            variant="outline"
            disabled={targetUserId === null || createManual.isPending}
            onClick={() => void onManual()}
          >
            Enter figures without a PDF
          </Button>
        </div>
        {uploading ? (
          <p role="status" className="text-sm">
            Reading tax forms…
          </p>
        ) : null}
        {outcomes.length > 0 ? (
          <ul
            className="flex flex-col gap-1 text-sm"
            data-testid="upload-outcomes"
          >
            {outcomes.map((o, i) => (
              <li
                key={`${o.name}-${i}`}
                className={
                  o.status === "parsed" || o.status === "duplicate"
                    ? ""
                    : "text-destructive"
                }
              >
                <span className="font-medium">{o.name}</span>: {o.message}
                {o.documentId !== undefined ? (
                  <Button
                    size="sm"
                    variant="outline"
                    className="ml-2"
                    onClick={() => openAdd(o.documentId!)}
                  >
                    Add figures manually
                  </Button>
                ) : null}
                {o.details.length > 0 ? (
                  <ul className="list-disc pl-5 text-xs text-muted-foreground">
                    {o.details.map((t, j) => (
                      <li key={j}>{t}</li>
                    ))}
                  </ul>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      {documents.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No tax documents imported yet.
        </p>
      ) : (
        <ul className="flex flex-col divide-y divide-border rounded-md border border-border">
          {documents.map((d) => (
            <DocumentRow key={d.id} d={d} users={users} request={request} />
          ))}
        </ul>
      )}
    </div>
  );
}
