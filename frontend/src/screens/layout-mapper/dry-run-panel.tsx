import * as React from "react";

import { Money } from "@/components/primitives/money";

import { useDryRunLayoutSpec } from "./api";

const DEBOUNCE_MS = 400;

/**
 * Live "this yields N transactions totalling X" feedback (§2d.2) via `POST /layout-specs/dry-run`
 * (§3.12) — no side effects, unlike `TestExtractPanel`'s `/extract`/`/reparse` run. Debounced on
 * `specYaml` so it does not fire on every keystroke.
 */
export function DryRunPanel({
  statementId,
  specYaml,
  enabled,
}: {
  statementId: number;
  specYaml: string;
  enabled: boolean;
}) {
  const [debouncedYaml, setDebouncedYaml] = React.useState(specYaml);
  const dryRun = useDryRunLayoutSpec();

  React.useEffect(() => {
    const handle = setTimeout(() => setDebouncedYaml(specYaml), DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [specYaml]);

  const { mutate } = dryRun;
  React.useEffect(() => {
    if (!enabled || debouncedYaml.trim() === "") return;
    mutate({ statementId, specYaml: debouncedYaml });
  }, [enabled, statementId, debouncedYaml, mutate]);

  if (!enabled) return null;
  if (dryRun.isPending) {
    return <p className="text-xs text-muted-foreground">Running a live preview…</p>;
  }
  if (!dryRun.data) return null;

  if (!dryRun.data.ok) {
    return (
      <ul className="flex flex-col gap-1 rounded-md border border-destructive/50 bg-destructive/5 p-2">
        {dryRun.data.fieldErrors.map((e) => (
          <li key={e.field + e.message} className="text-xs text-destructive">
            {e.field}: {e.message}
          </li>
        ))}
      </ul>
    );
  }

  const { result } = dryRun.data;
  return (
    <div className="flex flex-col gap-1 rounded-md border border-border bg-muted/30 p-2">
      <p className="text-sm font-medium">
        {result.txn_count} transaction{result.txn_count === 1 ? "" : "s"} totalling{" "}
        <Money tone="neutral" minorUnits={result.total_minor} currency={result.currency} />
        {result.reconciliation_delta_minor != null ? (
          <>
            {" "}
            (reconciliation delta{" "}
            <Money tone="neutral" minorUnits={result.reconciliation_delta_minor} currency={result.currency} />)
          </>
        ) : null}
      </p>
      {result.warnings.length > 0 ? (
        <ul className="flex flex-col gap-0.5">
          {result.warnings.map((w) => (
            <li key={w} className="text-xs text-muted-foreground">
              ⚠ {w}
            </li>
          ))}
        </ul>
      ) : null}
      {result.sample_rows.length > 0 ? (
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">Sample rows ({result.sample_rows.length})</summary>
          <pre className="mt-1 overflow-auto rounded bg-card p-1 font-mono text-xs">
            {JSON.stringify(result.sample_rows, null, 2)}
          </pre>
        </details>
      ) : null}
    </div>
  );
}
