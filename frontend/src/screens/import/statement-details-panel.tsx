import * as React from "react";
import { Link } from "react-router-dom";

import type { components } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

import { usePatchAccountCurrency, usePatchStatementDetails } from "./imported-statements-api";
import { formatPeriod, plural } from "./statement-format";
import { errorMessage, useSafeToast } from "./use-safe-toast";

type Statement = components["schemas"]["Statement"];
type Account = components["schemas"]["Account"];
type Issuer = components["schemas"]["Issuer"];

const MAX_SPAN_DAYS = 400;
const CURRENCY_RE = /^[A-Z]{3}$/;

export interface StatementDetailsPanelProps {
  statement: Statement;
  /** Every account the app knows; the ones of this statement's owner are offered as move targets. */
  accounts: readonly Account[];
  issuers: readonly Issuer[];
  /** How many statements (in the list the panel has) sit on this statement's account. */
  accountStatementCount: number;
  onClose: () => void;
}

function daysBetween(start: string, end: string): number {
  return Math.round((Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86_400_000);
}

/** A client-side check mirroring the server's period rules, so the obvious mistakes never leave. */
function validatePeriod(start: string, end: string): string | null {
  if (!start || !end) return "Enter both dates.";
  if (start > end) return "The start date must not be after the end date.";
  if (daysBetween(start, end) > MAX_SPAN_DAYS) return `A statement period can span at most ${MAX_SPAN_DAYS} days.`;
  return null;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2 border-t border-border pt-3 first:border-t-0 first:pt-0" aria-label={title}>
      <h5 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h5>
      {children}
    </section>
  );
}

function SectionError({ error }: { error: unknown }) {
  return (
    <p role="alert" className="text-xs text-destructive">
      {errorMessage(error)}
    </p>
  );
}

const fieldClass = "h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground";

function PeriodSection({ statement }: { statement: Statement }) {
  const toast = useSafeToast();
  const patch = usePatchStatementDetails();
  const [start, setStart] = React.useState(statement.period_start ?? "");
  const [end, setEnd] = React.useState(statement.period_end ?? "");
  // A refetch after a save (or a revert) brings the effective period; follow it.
  React.useEffect(() => {
    setStart(statement.period_start ?? "");
    setEnd(statement.period_end ?? "");
  }, [statement.period_start, statement.period_end]);

  const changed = start !== (statement.period_start ?? "") || end !== (statement.period_end ?? "");
  const invalid = changed ? validatePeriod(start, end) : null;
  const busy = patch.isPending;

  const save = () => {
    if (invalid) return;
    patch.mutate(
      { statementId: statement.id, body: { period_start: start, period_end: end } },
      { onSuccess: () => toast({ title: "Period updated", description: formatPeriod(start, end), variant: "success" }) },
    );
  };
  const revert = () =>
    patch.mutate(
      { statementId: statement.id, body: { revert_period: true } },
      { onSuccess: () => toast({ title: "Detected period restored", variant: "success" }) },
    );

  return (
    <Section title="Period">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
          Start
          <input
            type="date"
            className={fieldClass}
            value={start}
            onChange={(e) => setStart(e.target.value)}
            disabled={busy}
            aria-invalid={invalid ? true : undefined}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
          End
          <input
            type="date"
            className={fieldClass}
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            disabled={busy}
            aria-invalid={invalid ? true : undefined}
          />
        </label>
        <Button size="sm" onClick={save} disabled={busy || !changed || invalid !== null} aria-busy={busy}>
          {busy ? <Spinner /> : null}
          Save period
        </Button>
        {statement.period_corrected ? (
          <Button size="sm" variant="outline" onClick={revert} disabled={busy}>
            Restore detected period
          </Button>
        ) : null}
      </div>
      {statement.period_corrected ? (
        <p className="text-xs text-muted-foreground">
          <Badge variant="outline" className="mr-1">
            Corrected
          </Badge>
          Detected from the statement: {formatPeriod(statement.parsed_period_start ?? null, statement.parsed_period_end ?? null)}.
        </p>
      ) : null}
      <p className="text-xs text-muted-foreground">
        Changing the period does not change transaction dates; fix dates on the rows, or re-import with another
        extractor.
      </p>
      {invalid ? (
        <p role="alert" className="text-xs text-destructive">
          {invalid}
        </p>
      ) : null}
      {patch.isError ? <SectionError error={patch.error} /> : null}
    </Section>
  );
}

function CurrencySection({
  statement,
  account,
  accountName,
  count,
}: {
  statement: Statement;
  account: Account | undefined;
  accountName: string;
  count: number;
}) {
  const toast = useSafeToast();
  const patch = usePatchAccountCurrency();
  const current = account?.currency ?? statement.currency ?? "";
  const [value, setValue] = React.useState(current);
  const [confirming, setConfirming] = React.useState(false);
  React.useEffect(() => setValue(current), [current]);

  const normalized = value.trim().toUpperCase();
  const valid = CURRENCY_RE.test(normalized);
  const changed = normalized !== current;
  const busy = patch.isPending;

  const apply = () => {
    if (!account || !valid || !changed) return;
    patch.mutate(
      { accountId: account.id, currency: normalized },
      {
        onSuccess: () => {
          setConfirming(false);
          toast({ title: `Currency changed to ${normalized}`, description: accountName, variant: "success" });
        },
        onError: () => setConfirming(false),
      },
    );
  };
  const click = () => {
    if (count > 1 && !confirming) setConfirming(true);
    else apply();
  };

  return (
    <Section title="Currency">
      {account ? (
        <>
          <p className="text-xs text-muted-foreground">
            Currency of account <span className="font-medium text-foreground">{accountName}</span> ({plural(count, "statement")}).
            Amounts are not converted; only the label changes.
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-xs font-medium text-muted-foreground">
              Currency code
              <input
                className={`${fieldClass} w-24 uppercase`}
                value={value}
                maxLength={3}
                onChange={(e) => {
                  setValue(e.target.value);
                  setConfirming(false);
                }}
                disabled={busy}
                placeholder="USD"
                aria-invalid={value !== "" && !valid ? true : undefined}
              />
            </label>
            <Button size="sm" onClick={click} disabled={busy || !valid || !changed} aria-busy={busy}>
              {busy ? <Spinner /> : null}
              {confirming ? `Yes, change ${plural(count, "statement")}` : "Change currency"}
            </Button>
          </div>
          {confirming ? (
            <p role="status" className="text-xs text-amber-700 dark:text-amber-400">
              This account has {count} statements. All of them, and every transaction on the account, will be relabeled{" "}
              {normalized}.
            </p>
          ) : null}
          {value !== "" && !valid ? (
            <p role="alert" className="text-xs text-destructive">
              Use a three-letter code such as USD or EUR.
            </p>
          ) : null}
          {patch.isError ? <SectionError error={patch.error} /> : null}
        </>
      ) : (
        <p className="text-xs text-muted-foreground">This statement has no account yet, so there is no currency to change.</p>
      )}
    </Section>
  );
}

function BankSection({
  statement,
  accounts,
  issuers,
  accountLabel,
}: {
  statement: Statement;
  accounts: readonly Account[];
  issuers: readonly Issuer[];
  accountLabel: (a: Account) => string;
}) {
  const toast = useSafeToast();
  const bankPatch = usePatchStatementDetails();
  const accountPatch = usePatchStatementDetails();
  const otherIssuers = issuers.filter((i) => i.id !== statement.issuer_id);
  const targets = accounts.filter((a) => a.user_id === statement.user_id && a.id !== statement.account_id);
  const [issuerId, setIssuerId] = React.useState<number | "">(otherIssuers[0]?.id ?? "");
  const [accountId, setAccountId] = React.useState<number | "">(targets[0]?.id ?? "");
  // Keep the selections valid when the lists change underneath (a move changes both).
  React.useEffect(() => {
    if (issuerId === "" || !otherIssuers.some((i) => i.id === issuerId)) setIssuerId(otherIssuers[0]?.id ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [otherIssuers.map((i) => i.id).join(",")]);
  React.useEffect(() => {
    if (accountId === "" || !targets.some((a) => a.id === accountId)) setAccountId(targets[0]?.id ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targets.map((a) => a.id).join(",")]);

  const report = (result: components["schemas"]["StatementPatchResult"], what: string) =>
    toast({
      title: `Moved ${plural(result.rows_moved, "row")}, merged ${plural(result.duplicates_deleted, "duplicate")}`,
      description: what,
      variant: "success",
    });

  const issuerName = issuers.find((i) => i.id === statement.issuer_id)?.name;

  return (
    <Section title="Bank">
      <p className="text-xs text-muted-foreground">
        {issuerName ? `Read as ${issuerName}. ` : ""}Pick the right bank if it was recognised wrongly. The statement and its
        transactions move to that bank&rsquo;s matching account (created if missing); transactions already there are not
        duplicated.
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex min-w-0 max-w-xs flex-col gap-1 text-xs font-medium text-muted-foreground">
          Bank
          <select
            className={fieldClass}
            value={issuerId}
            onChange={(e) => setIssuerId(Number(e.target.value))}
            disabled={bankPatch.isPending || otherIssuers.length === 0}
          >
            {otherIssuers.map((i) => (
              <option key={i.id} value={i.id}>
                {i.name}
              </option>
            ))}
          </select>
        </label>
        <Button
          size="sm"
          disabled={bankPatch.isPending || issuerId === ""}
          aria-busy={bankPatch.isPending}
          onClick={() =>
            issuerId !== "" &&
            bankPatch.mutate(
              { statementId: statement.id, body: { issuer_id: issuerId } },
              { onSuccess: (r) => report(r, `Now under ${issuers.find((i) => i.id === issuerId)?.name ?? "the new bank"}`) },
            )
          }
        >
          {bankPatch.isPending ? <Spinner /> : null}
          Change bank
        </Button>
      </div>
      {bankPatch.isError ? <SectionError error={bankPatch.error} /> : null}
      <p className="text-xs text-muted-foreground">
        To have the next file from this bank recognised, add its names under{" "}
        <Link to="/spending/rules" className="underline underline-offset-4">
          Rules &rarr; Issuers
        </Link>
        .
      </p>

      {targets.length > 0 ? (
        <>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex min-w-0 max-w-xs flex-col gap-1 text-xs font-medium text-muted-foreground">
              Move to an existing account
              <select
                className={fieldClass}
                value={accountId}
                onChange={(e) => setAccountId(Number(e.target.value))}
                disabled={accountPatch.isPending}
              >
                {targets.map((a) => (
                  <option key={a.id} value={a.id}>
                    {accountLabel(a)}
                  </option>
                ))}
              </select>
            </label>
            <Button
              size="sm"
              variant="outline"
              disabled={accountPatch.isPending || accountId === ""}
              aria-busy={accountPatch.isPending}
              onClick={() =>
                accountId !== "" &&
                accountPatch.mutate(
                  { statementId: statement.id, body: { account_id: accountId } },
                  {
                    onSuccess: (r) =>
                      report(r, `Now on ${accountLabel(accounts.find((a) => a.id === accountId) as Account)}`),
                  },
                )
              }
            >
              {accountPatch.isPending ? <Spinner /> : null}
              Move statement
            </Button>
          </div>
          {accountPatch.isError ? <SectionError error={accountPatch.error} /> : null}
        </>
      ) : null}
    </Section>
  );
}

/** "Edit details…" under an imported statement: fix the period, the account's currency, or the bank
 * it was filed under. Each section saves on its own and reports server refusals in place. */
export function StatementDetailsPanel({
  statement,
  accounts,
  issuers,
  accountStatementCount,
  onClose,
}: StatementDetailsPanelProps) {
  const boxRef = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    boxRef.current?.focus();
  }, []);
  const name = statement.original_name || `statement ${statement.id}`;
  const issuerName = new Map(issuers.map((i) => [i.id, i.name]));
  const accountLabel = (a: Account) =>
    [a.issuer_id != null ? issuerName.get(a.issuer_id) : undefined, a.account_type, `(${a.currency})`].filter(Boolean).join(" ");
  const account = accounts.find((a) => a.id === statement.account_id);

  return (
    <div
      ref={boxRef}
      role="region"
      aria-label={`Edit details of ${name}`}
      tabIndex={-1}
      data-testid="statement-details-panel"
      className="flex max-h-[70vh] flex-col gap-3 overflow-y-auto rounded-md border border-border bg-muted/30 p-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onClose();
        }
      }}
    >
      <PeriodSection statement={statement} />
      <CurrencySection
        statement={statement}
        account={account}
        accountName={account ? accountLabel(account).replace(/ \([A-Z]{3}\)$/, "") || `account ${account.id}` : ""}
        count={Math.max(accountStatementCount, 1)}
      />
      <BankSection statement={statement} accounts={accounts} issuers={issuers} accountLabel={accountLabel} />
      <div>
        <Button size="sm" variant="ghost" onClick={onClose}>
          Done
        </Button>
      </div>
    </div>
  );
}
