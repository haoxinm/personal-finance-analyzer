import * as React from "react";

import { validateSpec } from "./spec-validate";
import type { SpecFieldError } from "./spec-types";
import { parseYamlish, YamlParseError } from "./yaml";

export interface PasteSpecResult {
  text: string;
  parsed: unknown;
  parseError: string | null;
  errors: SpecFieldError[];
}

/**
 * "Paste a layout spec" (§2d.2 step 4, A19): a spec obtained any other way — another user, a
 * future LLM proposal (§2e, Phase 5 only), documentation — goes through the same validation and
 * approval as a hand-mapped one. There is deliberately no way to bypass validation here.
 *
 * Owns its own textarea state and reports the parsed value (or the reason it could not be
 * parsed/validated) up to the caller on every change, via `onResult`.
 */
export function PasteSpecPanel({ onResult }: { onResult: (result: PasteSpecResult) => void }) {
  const [text, setText] = React.useState("");

  const result = React.useMemo<PasteSpecResult>(() => {
    if (text.trim() === "") {
      return { text, parsed: null, parseError: null, errors: [] };
    }
    try {
      const parsed = parseYamlish(text);
      return { text, parsed, parseError: null, errors: validateSpec(parsed) };
    } catch (exc) {
      const message = exc instanceof YamlParseError ? exc.message : String(exc);
      return { text, parsed: null, parseError: message, errors: [] };
    }
  }, [text]);

  React.useEffect(() => {
    onResult(result);
    // `onResult` is a plain setter from the caller (not a dependency worth re-running on).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result]);

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-muted-foreground">
        Paste a layout spec YAML (or JSON — valid YAML). It runs through the same field-level validation as the
        manual mapper before it can be saved.
      </p>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={16}
        spellCheck={false}
        className="w-full rounded-md border border-input bg-transparent p-3 font-mono text-xs"
        placeholder={"id: user_acme_credit\nversion: 1\naccount_type: credit\n..."}
      />
      {result.parseError ? (
        <p className="text-xs text-destructive">Could not parse: {result.parseError}</p>
      ) : result.errors.length > 0 ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3">
          <p className="text-xs font-medium text-destructive">{result.errors.length} problem(s) found:</p>
          <ul className="mt-1 list-inside list-disc text-xs text-destructive">
            {result.errors.map((e) => (
              <li key={e.field + e.message}>
                {e.field}: {e.message}
              </li>
            ))}
          </ul>
        </div>
      ) : result.parsed !== null ? (
        <p className="text-xs font-medium text-emerald-600 dark:text-emerald-400">Valid — no problems found.</p>
      ) : null}
    </div>
  );
}
