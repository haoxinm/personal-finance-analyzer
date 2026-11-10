import * as React from "react";
import { useSearchParams } from "react-router-dom";

import type { components } from "@/api/client";
import { UserSwitcher } from "@/components/primitives/user-switcher";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import { useToast } from "@/components/ui/use-toast";

import {
  ADD_PARAM,
  DOCUMENT_PARAM,
  useCreateManualDocument,
  useDeleteTaxDocument,
  useLlmReadAvailable,
  useReassignTaxDocument,
} from "../hooks";
import {
  documentStatusLabel,
  errorMessage,
  formTypeLabel,
  type TaxDocument,
  type TaxFormType,
} from "../logic";
import { DocumentDetail } from "./document-detail";
import { LlmReadSection } from "./llm-read-section";

type User = components["schemas"]["User"];

export interface DocumentsPanelProps {
  documents: readonly TaxDocument[];
  users: readonly User[];
  /** Selected user, or null for "All users". */
  selectedUserId: number | null;
  /** Year filter (`?year=`): only that year's documents are listed; `null` lists every year. */
  year?: number | null;
  /** Documents holding a form that a corrected form replaced (not counted in any total):
   * document id -> the replaced forms' types. */
  superseded?: ReadonlyMap<number, readonly TaxFormType[]>;
}

function DocumentRow({
  d: rowDoc,
  users,
  replacedForms,
}: {
  d: TaxDocument;
  users: readonly User[];
  replacedForms?: readonly TaxFormType[];
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
  // Only offered for a PDF the user put in the documents folder themselves: it is kept otherwise.
  const [alsoDeletePdf, setAlsoDeletePdf] = React.useState(false);
  // Unique per rendered row: compare mode renders the same document in two panes.
  const uid = React.useId();
  const detailId = `tax-doc-detail-${d.id}-${uid}`;
  const llmId = `tax-doc-llm-${d.id}-${uid}`;
  const [llmOpen, setLlmOpen] = React.useState(false);
  const canReadWithLlm = useLlmReadAvailable(d);
  // `?doc=<id>&add=1` (from an upload outcome or manual creation) opens this row's add-form editor.
  // `add` is consumed once handled so a reload or a later remount does not reopen it.
  const [searchParams, setSearchParams] = useSearchParams();
  const rowRef = React.useRef<HTMLLIElement>(null);
  const addRequested =
    searchParams.get(DOCUMENT_PARAM) === String(d.id) &&
    searchParams.get(ADD_PARAM) === "1";

  React.useEffect(() => {
    if (!addRequested) return;
    setOpen(true);
    setAdding(true);
    rowRef.current?.scrollIntoView?.({ block: "nearest" });
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete(ADD_PARAM);
        return next;
      },
      { replace: true },
    );
  }, [addRequested, setSearchParams]);

  async function onDelete() {
    try {
      await remove.mutateAsync({
        id: d.id,
        deletePdf: d.pdf_in_place === true && alsoDeletePdf,
      });
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
    <li ref={rowRef} className="flex flex-col gap-2 p-3" data-testid="tax-document">
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
        {replacedForms && replacedForms.length > 0 ? (
          <Badge
            variant="outline"
            data-testid="tax-document-superseded"
            className="border-amber-500/50 text-amber-700 dark:text-amber-400"
            title={`${replacedForms.map(formTypeLabel).join(", ")} on this document ${replacedForms.length === 1 ? "was" : "were"} replaced by a corrected form with the same payer and account, so ${replacedForms.length === 1 ? "it is" : "they are"} not counted in your totals.`}
          >
            Replaced by corrected form
          </Badge>
        ) : null}
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
              {d.pdf_in_place === true ? (
                <label className="flex items-center gap-1 text-xs">
                  <input
                    type="checkbox"
                    checked={alsoDeletePdf}
                    onChange={(e) => setAlsoDeletePdf(e.target.checked)}
                  />
                  Also delete the PDF file
                  <span className="text-muted-foreground">
                    (your file in the documents folder; kept otherwise)
                  </span>
                </label>
              ) : null}
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
                onClick={() => {
                  setConfirming(false);
                  setAlsoDeletePdf(false);
                }}
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
  year = null,
  superseded,
}: DocumentsPanelProps) {
  const [searchParams] = useSearchParams();
  const requestedDoc = Number(searchParams.get(DOCUMENT_PARAM));
  // A year filter hides other years' documents, but never the one a deep link points at.
  const shown =
    year === null
      ? documents
      : documents.filter((d) => d.tax_year === year || d.id === requestedDoc);

  return (
    <div className="flex flex-col gap-4" data-testid="tax-documents">
      <ManualEntry
        users={users}
        selectedUserId={selectedUserId}
        defaultYear={year}
      />
      {documents.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No tax documents imported yet.
        </p>
      ) : shown.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No tax documents for {year}.
        </p>
      ) : (
        <ul className="flex flex-col divide-y divide-border rounded-md border border-border">
          {shown.map((d) => (
            <DocumentRow
              key={d.id}
              d={d}
              users={users}
              replacedForms={superseded?.get(d.id)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

/** "Enter figures without a PDF": creates an empty document, then opens its add-form editor. */
function ManualEntry({
  users,
  selectedUserId,
  defaultYear,
}: {
  users: readonly User[];
  selectedUserId: number | null;
  defaultYear: number | null;
}) {
  const createManual = useCreateManualDocument();
  const { toast } = useToast();
  const [, setSearchParams] = useSearchParams();
  // One concrete user is needed; with "All users" selected the user picks one here.
  const [forUser, setForUser] = React.useState<number | null>(null);
  const targetUserId = selectedUserId ?? forUser ?? users[0]?.id ?? null;
  const [taxYear, setTaxYear] = React.useState(
    defaultYear === null ? "" : String(defaultYear),
  );

  const yearNumber = taxYear.trim() === "" ? undefined : Number(taxYear);
  const yearInvalid =
    yearNumber !== undefined &&
    (!Number.isInteger(yearNumber) || yearNumber < 1990 || yearNumber > 2100);

  async function onManual() {
    if (targetUserId === null) return;
    if (yearNumber === undefined || yearInvalid) {
      toast({
        variant: "destructive",
        title: "Enter the tax year first",
        description: "Fill in the tax year field next to the button.",
      });
      return;
    }
    try {
      const doc = await createManual.mutateAsync({
        user_id: targetUserId,
        tax_year: yearNumber,
      });
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set(DOCUMENT_PARAM, String(doc.id));
          next.set(ADD_PARAM, "1");
          return next;
        },
        { replace: true },
      );
    } catch (e) {
      toast({
        variant: "destructive",
        title: "Could not create document",
        description: errorMessage(e),
      });
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-3">
      {selectedUserId === null && users.length > 1 ? (
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">Add for</span>
          <UserSwitcher
            users={users}
            value={targetUserId}
            onChange={setForUser}
            allowAll={false}
            variant="select"
            aria-label="Add for user"
          />
        </label>
      ) : null}
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        Tax year
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
      <Button
        size="sm"
        variant="outline"
        disabled={targetUserId === null || createManual.isPending}
        onClick={() => void onManual()}
      >
        Enter figures without a PDF
      </Button>
    </div>
  );
}
