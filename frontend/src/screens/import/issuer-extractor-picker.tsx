import * as React from "react";
import { Link } from "react-router-dom";

import { existingIssuerIdFromError } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import { useCreateIssuer, useImportSettings, useIssuers, useLayoutSpecs } from "./api";
import { extractorChoiceForIssuer } from "./use-import-queue";
import type { ExtractorChoice, ImportItem } from "./types";

export interface IssuerExtractorPickerProps {
  item: ImportItem;
  onIssuerChange: (issuerId: number, extractorChoice: ExtractorChoice | null) => void;
  onExtractorChange: (choice: ExtractorChoice) => void;
  /** A new issuer was created here (after it is selected): other pending files may now match it. */
  onIssuerCreated?: (issuerId: number) => void;
}

/** Issuer and extractor override (§2f.2/§2f.4): a select for the matched (or overridden) issuer,
 * an inline "new issuer" form seeded with `match_terms`, and a select for the extractor —
 * either the proposed built-in layout parser or one of that issuer's approved layout specs
 * (§2f.1). Both selects are one click to open; nothing here forces a dialog. */
export function IssuerExtractorPicker({
  item,
  onIssuerChange,
  onExtractorChange,
  onIssuerCreated,
}: IssuerExtractorPickerProps) {
  const issuersQuery = useIssuers();
  const specsQuery = useLayoutSpecs();
  const settingsQuery = useImportSettings();
  const createIssuer = useCreateIssuer();
  const [isCreatingIssuer, setIsCreatingIssuer] = React.useState(false);
  const [newIssuerName, setNewIssuerName] = React.useState("");

  const issuers = issuersQuery.data ?? [];
  const typedName = newIssuerName.trim();
  const nameMatch = typedName
    ? issuers.find((i) => i.name.toLowerCase() === typedName.toLowerCase())
    : undefined;
  // After a 409 the server names the existing issuer; fall back to the typed-name match.
  const existingId = existingIssuerIdFromError(createIssuer.error);
  const existingIssuer =
    (existingId !== null ? issuers.find((i) => i.id === existingId) : undefined) ?? nameMatch;
  const selectIssuer = (issuerId: number) =>
    onIssuerChange(issuerId, extractorChoiceForIssuer(issuerId, specs, fallbackParserId));
  const closeCreateForm = () => {
    setIsCreatingIssuer(false);
    setNewIssuerName("");
    createIssuer.reset();
  };
  const specs = specsQuery.data ?? [];
  const specsForIssuer = specs.filter((s) => s.issuer_id === item.issuerId && s.approved);
  const fallbackParserId = item.proposal?.parser_id ?? null;

  const llmMode = settingsQuery.data?.llm.mode;
  const llmUnavailable = llmMode === undefined || llmMode === "none";
  const builtInParserId = fallbackParserId !== null && fallbackParserId !== "llm" ? fallbackParserId : null;

  const extractorValue =
    item.extractorChoice === null
      ? ""
      : item.extractorChoice.kind === "parser"
        ? `parser:${item.extractorChoice.parserId}`
        : `spec:${item.extractorChoice.layoutSpecId}`;

  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:gap-4">
      <label className="flex flex-1 flex-col gap-1 text-xs font-medium text-muted-foreground">
        Issuer
        {isCreatingIssuer ? (
          <>
            <div className="flex gap-1.5">
              <Input
                autoFocus
                placeholder="Issuer name, e.g. Bank of America"
                value={newIssuerName}
                onChange={(event) => {
                  setNewIssuerName(event.target.value);
                  createIssuer.reset();
                }}
                aria-invalid={createIssuer.isError || undefined}
                className="h-8 text-sm"
              />
              <Button
                type="button"
                size="sm"
                disabled={typedName.length === 0 || createIssuer.isPending}
                onClick={() => {
                  createIssuer.mutate(
                    { name: typedName, match_terms: [typedName.toLowerCase()] },
                    {
                      onSuccess: (issuer) => {
                        closeCreateForm();
                        selectIssuer(issuer.id);
                        onIssuerCreated?.(issuer.id);
                      },
                    },
                  );
                }}
              >
                Add
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={closeCreateForm}>
                Cancel
              </Button>
            </div>
            {createIssuer.isError ? (
              <p role="alert" className="font-normal text-destructive">
                {createIssuer.error.message}
              </p>
            ) : nameMatch ? (
              <p className="font-normal">An issuer named '{nameMatch.name}' already exists.</p>
            ) : null}
            {existingIssuer && (createIssuer.isError || nameMatch) ? (
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="self-start"
                onClick={() => {
                  closeCreateForm();
                  selectIssuer(existingIssuer.id);
                }}
              >
                Use existing '{existingIssuer.name}'
              </Button>
            ) : null}
          </>
        ) : (
          <div className="flex gap-1.5">
            <select
              className="h-8 flex-1 rounded-md border border-input bg-transparent px-2 text-sm text-foreground"
              value={item.issuerId ?? ""}
              onChange={(event) => {
                selectIssuer(Number(event.target.value));
              }}
            >
              <option value="" disabled>
                {item.issuerId === null ? "Select an issuer…" : ""}
              </option>
              {issuers.map((issuer) => (
                <option key={issuer.id} value={issuer.id}>
                  {issuer.name}
                </option>
              ))}
            </select>
            <Button type="button" size="sm" variant="outline" onClick={() => setIsCreatingIssuer(true)}>
              New
            </Button>
          </div>
        )}
      </label>

      <label className="flex flex-1 flex-col gap-1 text-xs font-medium text-muted-foreground">
        Extractor
        <select
          className="h-8 rounded-md border border-input bg-transparent px-2 text-sm text-foreground"
          value={extractorValue}
          disabled={item.issuerId === null}
          onChange={(event) => {
            const [kind, rest] = event.target.value.split(":", 2);
            if (kind === "parser" && rest) {
              onExtractorChange({ kind: "parser", parserId: rest });
            } else if (kind === "spec" && rest) {
              onExtractorChange({ kind: "spec", layoutSpecId: Number(rest) });
            }
          }}
        >
          <option value="" disabled>
            Select an extractor…
          </option>
          {builtInParserId !== null ? (
            <option value={`parser:${builtInParserId}`}>
              Auto-detected: {builtInParserId}
              {item.proposal ? ` (${Math.round(item.proposal.confidence * 100)}% match)` : ""}
            </option>
          ) : null}
          <option value="parser:llm" disabled={llmUnavailable && extractorValue !== "parser:llm"}>
            LLM (reads the selected pages)
          </option>
          {specsForIssuer.map((spec) => (
            <option key={spec.id} value={`spec:${spec.id}`}>
              {spec.name} v{spec.version}
            </option>
          ))}
        </select>
        {llmMode === "none" ? (
          <span className="font-normal">
            <Link to="/settings" className="underline">
              Set up an LLM in Settings
            </Link>{" "}
            to use the LLM extractor.
          </span>
        ) : null}
      </label>
    </div>
  );
}
