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

import { useImportSettings, useIssuers, useLayoutSpecs } from "./api";
import { useDeleteStatement } from "./imported-statements-api";
import { ReimportPanel } from "./reimport-panel";
import { formatPeriod, sortStatements } from "./statement-format";
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
}

function DeleteConfirm({ statement, onClose }: { statement: Statement; onClose: () => void }) {
  const toast = useSafeToast();
  const del = useDeleteStatement();
  const [deletePdf, setDeletePdf] = React.useState(false);
  const count = statement.txn_count ?? 0;
  const name = statement.original_name || `statement ${statement.id}`;

  return (
    <div
      role="alertdialog"
      aria-label={`Confirm delete ${name}`}
      className="flex flex-col gap-3 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm"
    >
      <p>
        Delete this statement and its {count.toLocaleString()} transaction{count === 1 ? "" : "s"}? Your category
        edits and notes on them are lost. This can&rsquo;t be undone.
      </p>
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
                  onClose();
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

type RowMode = "idle" | "reimport" | "delete";

function StatementRow({ statement, ctx }: { statement: Statement; ctx: RowContext }) {
  const [mode, setMode] = React.useState<RowMode>("idle");
  const name = statement.original_name || `Statement ${statement.id}`;
  const account = ctx.accountLabel(statement.account_id);
  const owner = ctx.ownerName(statement.user_id);
  const delta = statement.reconciliation_delta_minor;
  const parsed = statement.status === "parsed";
  const reimportBlocked = !statement.has_pdf;
  const reasonId = `no-pdf-${statement.id}`;

  return (
    <li className="flex flex-col gap-2 rounded-md border p-3 text-sm" data-testid="imported-statement">
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 max-w-full truncate font-medium" title={name}>
          {name}
        </span>
        <Badge variant={parsed ? "default" : "destructive"}>{STATUS_LABEL[statement.status]}</Badge>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {owner ? <span>{owner}</span> : null}
        {account ? <span>{account}</span> : null}
        <span>{formatPeriod(statement.period_start, statement.period_end)}</span>
        <span>
          {(statement.txn_count ?? 0).toLocaleString()} transaction{statement.txn_count === 1 ? "" : "s"}
        </span>
      </div>
      {parsed && delta != null && delta !== 0 ? (
        <p className="text-xs text-amber-700 dark:text-amber-400">
          Totals don&rsquo;t reconcile (off by <Money minorUnits={Math.abs(delta)} tone="neutral" />)
        </p>
      ) : null}
      {mode === "idle" ? (
        <div className="flex flex-wrap items-center gap-2">
          {parsed ? (
            <Button asChild size="sm" variant="outline">
              <Link to={`/spending/transactions?statement_id=${statement.id}`}>View transactions</Link>
            </Button>
          ) : null}
          {parsed ? (
            <Button
              size="sm"
              variant="outline"
              onClick={() => setMode("reimport")}
              disabled={reimportBlocked}
              aria-describedby={reimportBlocked ? reasonId : undefined}
              title={reimportBlocked ? NO_PDF_REASON : undefined}
              aria-label={`Re-import ${name}`}
            >
              Re-import…
            </Button>
          ) : null}
          <Button size="sm" variant="ghost" onClick={() => setMode("delete")} aria-label={`Delete ${name}`}>
            Delete…
          </Button>
        </div>
      ) : null}
      {parsed && reimportBlocked && mode === "idle" ? (
        <p id={reasonId} className="text-xs text-muted-foreground">
          {NO_PDF_REASON}
        </p>
      ) : null}
      {mode === "reimport" ? (
        <ReimportPanel
          statement={statement}
          specs={ctx.specs}
          llmConfigured={ctx.llmConfigured}
          onClose={() => setMode("idle")}
        />
      ) : null}
      {mode === "delete" ? <DeleteConfirm statement={statement} onClose={() => setMode("idle")} /> : null}
    </li>
  );
}

/**
 * "Imported statements": everything already imported for the selected user, with the two repairs
 * the Import queue cannot offer: Re-import (rebuild from the saved PDF, edits kept unless
 * discarded) and Delete.
 */
export function ImportedStatementsPanel({ userId }: { userId: number | null }) {
  const statements = useStatements(userId ?? undefined);
  const accounts = useAccounts();
  const issuers = useIssuers();
  const specs = useLayoutSpecs();
  const users = useUsers();
  const settings = useImportSettings();
  const [showAll, setShowAll] = React.useState(false);

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
    };
  }, [issuers.data, accounts.data, users.data, specs.data, settings.data, userId]);

  const rows = sortStatements(statements.data ?? []);
  const visible = showAll ? rows : rows.slice(0, INITIAL_ROWS);

  return (
    <Card data-testid="imported-statements-panel" role="region" aria-labelledby="imported-statements-title">
      <CardHeader>
        <CardTitle id="imported-statements-title">Imported statements</CardTitle>
        <CardDescription>Re-import a statement from its PDF to fix how it was read, or delete it.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {statements.isPending ? (
          <div className="flex flex-col gap-2" aria-busy="true">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : statements.isError ? (
          <ErrorState description="Could not load the imported statements." onRetry={() => void statements.refetch()} />
        ) : rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">No statements imported yet.</p>
        ) : (
          <>
            <ul className="flex flex-col gap-2">
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
