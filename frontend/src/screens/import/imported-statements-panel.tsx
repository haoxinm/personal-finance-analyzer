import { useQuery } from "@tanstack/react-query";
import * as React from "react";
import { Link } from "react-router-dom";

import { useStatements, useUsers } from "@/api/hooks";
import type { components } from "@/api/client";
import { Money } from "@/components/primitives/money";
import { ErrorState } from "@/components/primitives/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { useAccounts } from "@/screens/dashboard/hooks";

import { fetchStatement, useImportSettings, useIssuers, useLayoutSpecs } from "./api";
import { useDeleteStatement, usePatchStatementOwner } from "./imported-statements-api";
import { ReimportPanel } from "./reimport-panel";
import {
  PDF_MISSING_REASON,
  attentionNotes,
  formatPeriod,
  hasNoTransactions,
  periodsOverlap,
  plural,
  sortStatements,
  statementTransactionsUrl,
} from "./statement-format";
import { errorMessage, useSafeToast } from "./use-safe-toast";

type Statement = components["schemas"]["Statement"];

const INITIAL_ROWS = 10;
const NO_PDF_REASON = "No copy of the PDF was kept. Delete the statement and drop the PDF again.";

const STATUS_LABEL: Record<Statement["status"], string> = {
  pending: "Pending",
  awaiting_extractor: "Not imported yet",
  parsed: "Imported",
  no_text_layer: "Can't read this PDF",
  unsupported_layout: "Unrecognized layout",
  error: "Import failed",
};

interface RowContext {
  accountLabel: (accountId: number | null) => string | null;
  ownerName: (userId: number | null | undefined) => string | null;
  specs: readonly components["schemas"]["LayoutSpec"][];
  llmConfigured: boolean;
  onFinishImport?: (statement: Statement) => void;
  /** A request from elsewhere on the page (a dropped duplicate's "Re-import…") to open this row. */
  request?: OpenRequest | null;
  /** Take the request with this nonce: true exactly once per request, however often rows remount. */
  claimRequest: (nonce: number) => boolean;
  /** The person choices for "Change person…" (empty or one person: the action is not offered). */
  people: readonly { id: number; name: string }[];
  /** Other imported statements on the same account whose period overlaps this one's. */
  overlapping: (statement: Statement) => Statement[];
  defaultCurrency: string | undefined;
  /** Called after a statement is deleted, once its row is on its way out. */
  onDeleted: () => void;
}

/** Ask the panel to scroll to a statement's row and open its Re-import panel. `nonce` makes the
 * same statement requestable twice. */
export interface OpenRequest {
  statementId: number;
  nonce: number;
}

/** Statements that still need the user: awaiting an extractor, or whose import failed. */
const FINISHABLE: ReadonlySet<Statement["status"]> = new Set(["pending", "awaiting_extractor", "error", "unsupported_layout"]);

function DeleteConfirm({
  statement,
  overlapping,
  onClose,
  onDeleted,
}: {
  statement: Statement;
  overlapping: readonly Statement[];
  onClose: () => void;
  onDeleted: () => void;
}) {
  const boxRef = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    boxRef.current?.focus();
  }, []);
  const toast = useSafeToast();
  const del = useDeleteStatement();
  const [deletePdf, setDeletePdf] = React.useState(false);
  const count = statement.txn_count ?? 0;
  const name = statement.original_name || `statement ${statement.id}`;

  return (
    <div
      ref={boxRef}
      role="alertdialog"
      aria-label={`Confirm delete ${name}`}
      tabIndex={-1}
      className="flex flex-col gap-3 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
      onKeyDown={(e) => {
        if (e.key === "Escape" && !del.isPending) {
          e.stopPropagation();
          onClose();
        }
      }}
    >
      <p>
        Delete this statement and its {count.toLocaleString()} transaction{count === 1 ? "" : "s"}? Your category
        edits and notes on them are lost. This can&rsquo;t be undone.
      </p>
      {overlapping.length > 0 && count > 0 ? (
        <p className="text-xs text-amber-700 dark:text-amber-400" data-testid="delete-overlap-note">
          Transactions this statement shares with {overlapping.map((o) => o.original_name || `statement ${o.id}`).join(", ")}{" "}
          are stored only once, under this statement. Re-import {overlapping.length === 1 ? "that one" : "those"} afterwards to
          restore them.
        </p>
      ) : null}
      {statement.pdf_in_place ? (
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            className="h-4 w-4 accent-primary"
            checked={deletePdf}
            onChange={(e) => setDeletePdf(e.target.checked)}
            disabled={del.isPending}
          />
          Also delete the PDF file
        </label>
      ) : (
        <p className="text-xs text-muted-foreground">The app&rsquo;s copy of the PDF is removed.</p>
      )}
      {del.isError ? (
        <p role="alert" className="text-destructive">
          {errorMessage(del.error)}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="destructive"
          disabled={del.isPending}
          aria-busy={del.isPending}
          onClick={() =>
            del.mutate(
              { statementId: statement.id, deletePdf: statement.pdf_in_place && deletePdf },
              {
                onSuccess: () => {
                  toast({ title: "Statement deleted", description: name, variant: "success" });
                  onDeleted();
                },
              },
            )
          }
        >
          {del.isPending ? <Spinner /> : null}
          Delete statement
        </Button>
        <Button size="sm" variant="ghost" onClick={onClose} disabled={del.isPending}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function ChangePersonPanel({
  statement,
  people,
  onClose,
}: {
  statement: Statement;
  people: readonly { id: number; name: string }[];
  onClose: () => void;
}) {
  const boxRef = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    boxRef.current?.focus();
  }, []);
  const toast = useSafeToast();
  const patch = usePatchStatementOwner();
  const others = people.filter((p) => p.id !== statement.user_id);
  const [target, setTarget] = React.useState<number | "">(others[0]?.id ?? "");
  const name = statement.original_name || `statement ${statement.id}`;

  const move = () => {
    if (target === "") return;
    const person = people.find((p) => p.id === target);
    patch.mutate(
      { statementId: statement.id, userId: target },
      {
        onSuccess: (result) => {
          const merged = result.duplicates_deleted > 0 ? ` (${plural(result.duplicates_deleted, "duplicate")} merged)` : "";
          toast({
            title: `Moved to ${person?.name ?? "the other person"}: ${plural(result.rows_moved, "transaction")}${merged}`,
            description: name,
            variant: "success",
          });
          onClose();
        },
      },
    );
  };

  return (
    <div
      ref={boxRef}
      role="region"
      aria-label={`Change person for ${name}`}
      tabIndex={-1}
      className="flex flex-col gap-3 rounded-md border border-border bg-muted/30 p-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
      onKeyDown={(e) => {
        if (e.key === "Escape" && !patch.isPending) {
          e.stopPropagation();
          onClose();
        }
      }}
    >
      <p className="text-xs text-muted-foreground">
        Moves this statement and all its transactions to another person&rsquo;s matching account (created if they have
        none). Transactions they already have are not duplicated.
      </p>
      <label className="flex max-w-xs flex-col gap-1 text-xs font-medium text-muted-foreground">
        Move to
        <select
          className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground"
          value={target}
          onChange={(e) => setTarget(Number(e.target.value))}
          disabled={patch.isPending}
        >
          {others.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      {patch.isError ? (
        <p role="alert" className="text-destructive">
          {errorMessage(patch.error)}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={move} disabled={patch.isPending || target === ""} aria-busy={patch.isPending}>
          {patch.isPending ? <Spinner /> : null}
          Move statement
        </Button>
        <Button size="sm" variant="ghost" onClick={onClose} disabled={patch.isPending}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

type RowMode = "idle" | "reimport" | "delete" | "person";

function StatementRow({ statement, ctx, note }: { statement: Statement; ctx: RowContext; note?: string }) {
  const [mode, setMode] = React.useState<RowMode>("idle");
  const [highlight, setHighlight] = React.useState(false);
  const rowRef = React.useRef<HTMLLIElement>(null);
  const reimportBtn = React.useRef<HTMLButtonElement>(null);
  const deleteBtn = React.useRef<HTMLButtonElement>(null);
  const personBtn = React.useRef<HTMLButtonElement>(null);
  const returnTo = React.useRef<"reimport" | "delete" | "person" | null>(null);
  const name = statement.original_name || `Statement ${statement.id}`;
  const account = ctx.accountLabel(statement.account_id);
  const owner = ctx.ownerName(statement.user_id);
  const delta = statement.reconciliation_delta_minor;
  const parsed = statement.status === "parsed";
  const noRows = hasNoTransactions(statement);
  const pdfMissing = parsed && statement.pdf_missing;
  const reimportBlocked = !statement.has_pdf || statement.pdf_missing;
  const reasonId = `no-pdf-${statement.id}`;
  const unassigned = statement.user_id == null;
  const notes = attentionNotes(statement);
  const canChangePerson = parsed && ctx.people.length > 1;
  const showCurrency = Boolean(statement.currency) && ctx.defaultCurrency !== undefined && statement.currency !== ctx.defaultCurrency;

  const open = (next: "reimport" | "delete" | "person") => {
    returnTo.current = next;
    setMode(next);
  };
  const close = () => setMode("idle");

  // Back to the buttons: put focus on the one that opened the panel.
  React.useEffect(() => {
    if (mode !== "idle" || returnTo.current === null) return;
    const target = { reimport: reimportBtn, delete: deleteBtn, person: personBtn }[returnTo.current];
    target.current?.focus();
    returnTo.current = null;
  }, [mode]);

  // Asked from elsewhere on the page (a duplicate drop's "Re-import…"): scroll here and open it.
  // The panel hands each request out once, so a row remounting later (re-sort, "Show all", a
  // refetch) does not open it again.
  const requestNonce = ctx.request?.statementId === statement.id ? ctx.request.nonce : null;
  React.useEffect(() => {
    if (requestNonce === null || !ctx.claimRequest(requestNonce)) return;
    rowRef.current?.scrollIntoView?.({ block: "center", behavior: "smooth" });
    setHighlight(true);
    setTimeout(() => setHighlight(false), 3000);
    if (parsed && !reimportBlocked) open("reimport");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestNonce]);

  const badge = !parsed ? (
    <Badge variant="destructive">{STATUS_LABEL[statement.status]}</Badge>
  ) : noRows ? (
    <Badge variant="outline" className="border-amber-500/60 bg-amber-500/10 text-amber-800 dark:text-amber-300">
      No transactions found
    </Badge>
  ) : (
    <Badge variant="default">{STATUS_LABEL[statement.status]}</Badge>
  );

  return (
    <li
      ref={rowRef}
      className={`flex flex-col gap-2 rounded-md border p-3 text-sm transition-shadow ${highlight ? "ring-2 ring-ring" : ""} ${noRows ? "border-amber-500/50" : ""}`}
      data-testid="imported-statement"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 max-w-full truncate font-medium" title={name}>
          {name}
        </span>
        {badge}
        {unassigned ? <Badge variant="outline">No person yet</Badge> : null}
        {showCurrency ? <Badge variant="outline">{statement.currency}</Badge> : null}
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {owner ? <span>{owner}</span> : null}
        {account ? <span>{account}</span> : null}
        <span>{formatPeriod(statement.period_start, statement.period_end)}</span>
        <span>
          {(statement.txn_count ?? 0).toLocaleString()} transaction{statement.txn_count === 1 ? "" : "s"}
        </span>
      </div>
      {note ? (
        <p className="text-xs text-muted-foreground" data-testid="pinned-note">
          {note}
        </p>
      ) : null}
      {parsed && delta != null && delta !== 0 ? (
        <p className="text-xs text-amber-700 dark:text-amber-400">
          Totals don&rsquo;t reconcile (off by <Money minorUnits={Math.abs(delta)} tone="neutral" />)
        </p>
      ) : null}
      {notes.map((note) => (
        <p
          key={`${note.code}-${note.text}`}
          id={note.code === "pdf_missing" ? reasonId : undefined}
          className={`text-xs ${note.tone === "warn" ? "text-amber-700 dark:text-amber-400" : "text-muted-foreground"}`}
          data-testid={`attention-${note.code}`}
        >
          {note.text}
        </p>
      ))}
      {!parsed && statement.error_detail ? (
        <p className="text-xs text-muted-foreground">{statement.error_detail}</p>
      ) : null}
      {mode === "idle" ? (
        <div className="flex flex-wrap items-center gap-2">
          {parsed ? (
            <Button asChild size="sm" variant="outline">
              <Link to={statementTransactionsUrl(statement.id)}>View transactions</Link>
            </Button>
          ) : null}
          {FINISHABLE.has(statement.status) && ctx.onFinishImport ? (
            <Button
              size="sm"
              variant="outline"
              onClick={() => ctx.onFinishImport?.(statement)}
              aria-label={`Finish import of ${name}`}
            >
              Finish import
            </Button>
          ) : null}
          {parsed ? (
            <Button
              ref={reimportBtn}
              size="sm"
              variant={noRows && !reimportBlocked ? "default" : "outline"}
              onClick={() => open("reimport")}
              disabled={reimportBlocked}
              aria-describedby={reimportBlocked ? reasonId : undefined}
              title={reimportBlocked ? (pdfMissing ? PDF_MISSING_REASON : NO_PDF_REASON) : undefined}
              aria-label={`Re-import ${name}`}
            >
              Re-import…
            </Button>
          ) : null}
          {canChangePerson ? (
            <Button
              ref={personBtn}
              size="sm"
              variant="outline"
              onClick={() => open("person")}
              aria-label={`Change person for ${name}`}
            >
              Change person…
            </Button>
          ) : null}
          <Button ref={deleteBtn} size="sm" variant="ghost" onClick={() => open("delete")} aria-label={`Delete ${name}`}>
            Delete…
          </Button>
        </div>
      ) : null}
      {parsed && reimportBlocked && mode === "idle" && !notes.some((n) => n.code === "pdf_missing") ? (
        <p id={reasonId} className="text-xs text-muted-foreground">
          {pdfMissing ? PDF_MISSING_REASON : NO_PDF_REASON}
        </p>
      ) : null}
      {mode === "reimport" ? (
        <ReimportPanel
          statement={statement}
          specs={ctx.specs}
          llmConfigured={ctx.llmConfigured}
          onClose={close}
        />
      ) : null}
      {mode === "person" ? <ChangePersonPanel statement={statement} people={ctx.people} onClose={close} /> : null}
      {mode === "delete" ? (
        <DeleteConfirm
          statement={statement}
          overlapping={ctx.overlapping(statement)}
          onClose={close}
          onDeleted={() => {
            returnTo.current = null;
            ctx.onDeleted();
          }}
        />
      ) : null}
    </li>
  );
}

/**
 * "Imported statements": everything already imported for the selected user, with the repairs
 * the Import queue cannot offer: Re-import (rebuild from the saved PDF, edits kept unless
 * discarded), Change person and Delete. A statement that read nothing is flagged here, not
 * shown as a success.
 */
export function ImportedStatementsPanel({
  userId,
  onFinishImport,
  request,
  onRequestHandled,
}: {
  userId: number | null;
  onFinishImport?: (statement: Statement) => void;
  request?: OpenRequest | null;
  /** Called once a request has been taken by its row, so the owner can clear it. */
  onRequestHandled?: (nonce: number) => void;
}) {
  // The API's `user_id` is the account's owner, or the person an unfinished statement was uploaded
  // for, so one per-user query lists everything that belongs to this person.
  const statements = useStatements(userId ?? undefined);
  const accounts = useAccounts();
  const issuers = useIssuers();
  const specs = useLayoutSpecs();
  const users = useUsers();
  const settings = useImportSettings();
  const [showAll, setShowAll] = React.useState(false);
  const handledNonces = React.useRef(new Set<number>());
  const onHandledRef = React.useRef(onRequestHandled);
  onHandledRef.current = onRequestHandled;

  const rows = React.useMemo(() => sortStatements(statements.data ?? []), [statements.data]);

  // A request for a statement outside the current person's list (a folder problem that belongs to
  // someone else): fetch it and show it pinned above the list, so "Re-import…" always lands
  // somewhere. If it cannot be found, say so rather than do nothing.
  const toast = useSafeToast();
  const [pinnedId, setPinnedId] = React.useState<number | null>(null);
  const requestedStatementId = request?.statementId;
  const listLoaded = statements.isSuccess;
  const inList = requestedStatementId !== undefined && rows.some((r) => r.id === requestedStatementId);
  React.useEffect(() => {
    if (requestedStatementId === undefined || !listLoaded) return;
    setPinnedId(inList ? null : requestedStatementId);
  }, [requestedStatementId, listLoaded, inList]);
  const pinnedQuery = useQuery({
    queryKey: ["statement", pinnedId],
    enabled: pinnedId !== null,
    queryFn: () => fetchStatement(pinnedId as number),
  });
  const pinned = pinnedId !== null && !rows.some((r) => r.id === pinnedId) ? (pinnedQuery.data ?? null) : null;
  const missingRequest = React.useRef<number | null>(null);
  React.useEffect(() => {
    if (pinnedId === null || !pinnedQuery.isFetched || pinnedQuery.data) return;
    if (missingRequest.current === pinnedId) return;
    missingRequest.current = pinnedId;
    toast({
      title: "Could not find that statement",
      description: "It may have been deleted. Nothing was re-imported.",
      variant: "destructive",
    });
    onHandledRef.current?.(request?.nonce ?? -1);
    setPinnedId(null);
  }, [pinnedId, pinnedQuery.isFetched, pinnedQuery.data, toast, request?.nonce]);

  const ctx = React.useMemo<RowContext>(() => {
    const issuerName = new Map((issuers.data ?? []).map((i) => [i.id, i.name]));
    const accountById = new Map((accounts.data ?? []).map((a) => [a.id, a]));
    const userName = new Map((users.data ?? []).map((u) => [u.id, u.name]));
    return {
      accountLabel: (id) => {
        if (id === null) return null;
        const account = accountById.get(id);
        if (!account) return null;
        const issuer = account.issuer_id != null ? issuerName.get(account.issuer_id) : undefined;
        return [issuer, account.account_type].filter(Boolean).join(" ");
      },
      ownerName: (id) => (userId === null && id != null ? (userName.get(id) ?? null) : null),
      specs: specs.data ?? [],
      llmConfigured: settings.data !== undefined && settings.data.llm.mode !== "none",
      onFinishImport,
      request,
      claimRequest: (nonce) => {
        if (handledNonces.current.has(nonce)) return false;
        handledNonces.current.add(nonce);
        onHandledRef.current?.(nonce);
        return true;
      },
      people: (users.data ?? []).map((u) => ({ id: u.id, name: u.name })),
      overlapping: (statement) =>
        statement.account_id == null
          ? []
          : rows.filter(
              (other) =>
                other.id !== statement.id &&
                other.status === "parsed" &&
                other.account_id === statement.account_id &&
                periodsOverlap(statement, other),
            ),
      defaultCurrency: settings.data?.ingest?.default_currency,
      onDeleted: () => document.getElementById("imported-statements-title")?.focus(),
    };
  }, [issuers.data, accounts.data, users.data, specs.data, settings.data, userId, onFinishImport, request, rows]);

  // A request for a row beyond the first page must be able to reach it.
  const requestedId = request?.statementId;
  React.useEffect(() => {
    if (requestedId !== undefined && rows.findIndex((r) => r.id === requestedId) >= INITIAL_ROWS) setShowAll(true);
  }, [requestedId, rows]);
  const visible = showAll ? rows : rows.slice(0, INITIAL_ROWS);

  return (
    <Card data-testid="imported-statements-panel" role="region" aria-labelledby="imported-statements-title">
      <CardHeader>
        <CardTitle id="imported-statements-title" tabIndex={-1} className="outline-none focus-visible:ring-2 focus-visible:ring-ring">Imported statements</CardTitle>
        <CardDescription>
          Re-import a statement from its PDF to fix how it was read, move it to another person, or delete it.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {statements.isPending ? (
          <div className="flex flex-col gap-2" aria-busy="true">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : statements.isError ? (
          <ErrorState description="Could not load the imported statements." onRetry={() => void statements.refetch()} />
        ) : rows.length === 0 && !pinned ? (
          <p className="text-sm text-muted-foreground">No statements imported yet.</p>
        ) : (
          <>
            <ul className="flex flex-col gap-2">
              {pinned ? (
                <StatementRow
                  key={`pinned-${pinned.id}`}
                  statement={pinned}
                  ctx={ctx}
                  note={`${(users.data ?? []).find((u) => u.id === pinned.user_id)?.name ?? "Another person"}\u2019s statement (not in the list below)`}
                />
              ) : null}
              {visible.map((statement) => (
                <StatementRow key={statement.id} statement={statement} ctx={ctx} />
              ))}
            </ul>
            {rows.length > INITIAL_ROWS ? (
              <Button
                size="sm"
                variant="ghost"
                className="self-start"
                onClick={() => setShowAll((v) => !v)}
                aria-expanded={showAll}
              >
                {showAll ? "Show fewer" : `Show all (${rows.length})`}
              </Button>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}
