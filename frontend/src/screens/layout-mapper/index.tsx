import * as React from "react";
import { useSearchParams } from "react-router-dom";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, LoadingState } from "@/components/primitives/states";

import { useStatement, useStatementsForDrift } from "./api";
import { computeDrift, findPreviousStatement } from "./drift";
import { DriftBanner } from "./drift-banner";
import { DryRunPanel } from "./dry-run-panel";
import { ManualMapperForm } from "./manual-mapper-form";
import { type PasteSpecResult, PasteSpecPanel } from "./paste-spec-panel";
import { SavePanel } from "./save-panel";
import { emptySpec, type LayoutSpecObject } from "./spec-types";
import { validateSpec } from "./spec-validate";
import { StatementPicker } from "./statement-picker";
import { StatementPreview } from "./statement-preview";
import { TestExtractPanel } from "./test-extract-panel";
import { stringifySpec } from "./yaml";

type MapperTab = "manual" | "paste";

/**
 * The layout mapper (P3-G, §2d.2): for a statement no built-in parser could confidently read
 * (`unsupported_layout` / low `detect_score`), map columns manually or paste a spec obtained
 * elsewhere, save it as a local layout spec, and optionally contribute it upstream. No AI button
 * (A32/D13 — that is P5-C, a later, optional phase).
 *
 * Click-to-map (`StatementPreview`, `GET /statements/{id}/preview`) and the live "N transactions
 * totalling X" feedback (`DryRunPanel`, `POST /layout-specs/dry-run`) both come from §3.12.
 *
 * Opened with no `?statement_id=` (a direct visit, not the usual deep link), this screen shows
 * `StatementPicker` — a list of the user's statements, `unsupported_layout` /
 * `awaiting_extractor` ones called out first — rather than telling the person to hand-edit the
 * URL.
 */
export default function LayoutMapperScreen() {
  const [searchParams] = useSearchParams();
  const statementIdParam = searchParams.get("statement_id");
  const statementId = statementIdParam ? Number(statementIdParam) : null;
  const statementIdValid = statementId != null && Number.isInteger(statementId);

  const statementQuery = useStatement(statementIdValid ? statementId : null);
  const statement = statementQuery.data ?? null;

  const statementsForDrift = useStatementsForDrift(statement?.user_id ?? null);

  const [tab, setTab] = React.useState<MapperTab>("manual");
  const [manualSpec, setManualSpec] = React.useState<LayoutSpecObject>(emptySpec);
  const [pasteResult, setPasteResult] = React.useState<PasteSpecResult | null>(null);
  const [issuerId, setIssuerId] = React.useState("");
  const [savedSpecId, setSavedSpecId] = React.useState<number | null>(null);
  const [activeColumnIndex, setActiveColumnIndex] = React.useState<number | null>(null);

  const activeSpec: LayoutSpecObject | null =
    tab === "manual"
      ? manualSpec
      : pasteResult && pasteResult.errors.length === 0 && pasteResult.parsed !== null
        ? (pasteResult.parsed as LayoutSpecObject)
        : null;
  const activeErrors = tab === "manual" ? validateSpec(manualSpec) : pasteResult?.errors ?? [];
  const activeYaml =
    tab === "manual" ? stringifySpec(manualSpec) : (pasteResult?.text ?? "");

  if (!statementIdValid) {
    return (
      <div className="flex flex-col gap-4 py-2">
        <h1 className="text-xl font-semibold tracking-tight">Layout mapper</h1>
        <StatementPicker />
      </div>
    );
  }

  if (statementQuery.isLoading) {
    return <LoadingState />;
  }

  if (statementQuery.isError || statement === null) {
    return (
      <ErrorState
        title="Could not load this statement"
        description={statementQuery.error instanceof Error ? statementQuery.error.message : undefined}
        onRetry={() => void statementQuery.refetch()}
      />
    );
  }

  const previous = statementsForDrift.data ? findPreviousStatement(statementsForDrift.data, statement) : null;
  const drift = computeDrift(statement, previous ?? null);

  return (
    <div className="flex flex-col gap-4 py-2">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Layout mapper</h1>
        <p className="text-sm text-muted-foreground">
          Statement #{statement.id} — status: {statement.status}
          {statement.detect_score != null ? `, best parser confidence ${statement.detect_score.toFixed(2)}` : ""}
        </p>
      </div>

      <DriftBanner drift={drift} />

      {tab === "manual" ? (
        <Card>
          <CardHeader>
            <CardTitle>Statement preview</CardTitle>
          </CardHeader>
          <CardContent>
            <StatementPreview
              statementId={statement.id}
              activeColumn={activeColumnIndex != null ? (manualSpec.columns[activeColumnIndex] ?? null) : null}
              onMapColumn={(bounds) => {
                if (activeColumnIndex == null) return;
                setManualSpec({
                  ...manualSpec,
                  columns: manualSpec.columns.map((c, i) =>
                    i === activeColumnIndex ? { ...c, ...bounds } : c,
                  ),
                });
                setActiveColumnIndex(null);
              }}
            />
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>Build a layout spec</CardTitle>
          <div className="flex gap-1">
            <TabButton active={tab === "manual"} onClick={() => setTab("manual")}>
              Map columns
            </TabButton>
            <TabButton active={tab === "paste"} onClick={() => setTab("paste")}>
              Paste a layout spec
            </TabButton>
          </div>
        </CardHeader>
        <CardContent>
          {tab === "manual" ? (
            <ManualMapperForm
              spec={manualSpec}
              onChange={setManualSpec}
              activeColumnIndex={activeColumnIndex}
              onSetActiveColumn={setActiveColumnIndex}
            />
          ) : (
            <PasteSpecPanel onResult={setPasteResult} />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Live preview</CardTitle>
        </CardHeader>
        <CardContent>
          <DryRunPanel
            statementId={statement.id}
            specYaml={activeYaml}
            enabled={activeSpec !== null && activeErrors.length === 0}
          />
        </CardContent>
      </Card>

      <SavePanel
        spec={activeSpec}
        specErrors={activeErrors}
        specYaml={activeYaml}
        issuerId={issuerId}
        onIssuerIdChange={setIssuerId}
        onSaved={setSavedSpecId}
      />

      <TestExtractPanel
        statement={statement}
        layoutSpecId={savedSpecId}
        issuerId={issuerId ? Number(issuerId) : null}
      />
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={
        active
          ? "rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
          : "rounded-md px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted"
      }
    >
      {children}
    </button>
  );
}

export const screenMeta = { path: "/spending/layout-mapper", title: "Layout mapper" };
