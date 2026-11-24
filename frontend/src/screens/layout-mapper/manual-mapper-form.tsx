import * as React from "react";

import { Input } from "@/components/ui/input";

import { ColumnsEditor } from "./columns-editor";
import { SectionsEditor } from "./sections-editor";
import { validateSpec } from "./spec-validate";
import {
  ACCOUNT_TYPES,
  SIGN_CONVENTIONS,
  YEAR_INFERENCES,
  type LayoutSpecObject,
  type SpecFieldError,
} from "./spec-types";

export interface ManualMapperFormResult {
  spec: LayoutSpecObject;
  errors: SpecFieldError[];
}

/**
 * The manual layout mapper (§2d.2 step 2): builds a `LayoutSpecObject` field by field. Column
 * boundaries can be typed in as PDF points, or set by clicking a word in the `StatementPreview`
 * click-to-map panel (`activeColumnIndex`/`onSetActiveColumn`, threaded through from `index.tsx`,
 * which owns the preview alongside this form).
 */
export function ManualMapperForm({
  spec,
  onChange,
  activeColumnIndex = null,
  onSetActiveColumn,
}: {
  spec: LayoutSpecObject;
  onChange: (spec: LayoutSpecObject) => void;
  activeColumnIndex?: number | null;
  onSetActiveColumn?: (index: number | null) => void;
}) {
  const errors = React.useMemo(() => validateSpec(spec), [spec]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-3">
        <LabeledInput label="Spec id (lowercase, snake_case)" error={fieldError(errors, "id")}>
          <Input
            value={spec.id}
            placeholder="user_acme_credit"
            onChange={(e) => onChange({ ...spec, id: e.target.value })}
            className="w-64"
          />
        </LabeledInput>
        <LabeledInput label="Account type" error={fieldError(errors, "account_type")}>
          <select
            value={spec.account_type}
            onChange={(e) => onChange({ ...spec, account_type: e.target.value as LayoutSpecObject["account_type"] })}
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
          >
            {ACCOUNT_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </LabeledInput>
        <LabeledInput label="Currency" error={fieldError(errors, "currency")}>
          <Input
            value={spec.currency ?? "USD"}
            onChange={(e) => onChange({ ...spec, currency: e.target.value.toUpperCase() })}
            className="w-20"
          />
        </LabeledInput>
      </div>

      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">Detection</h3>
        <p className="text-xs text-muted-foreground">
          Terms this spec's statements always contain, used to auto-detect it on future imports (§2f.2).
        </p>
        <div className="flex flex-wrap gap-3">
          <LabeledInput label="Must contain all of (comma-separated)">
            <Input
              value={spec.detect.all_of.join(", ")}
              onChange={(e) =>
                onChange({
                  ...spec,
                  detect: { ...spec.detect, all_of: splitCommaList(e.target.value) },
                })
              }
              className="w-72"
            />
          </LabeledInput>
          <LabeledInput label="Must contain any of (comma-separated)">
            <Input
              value={spec.detect.any_of.join(", ")}
              onChange={(e) =>
                onChange({
                  ...spec,
                  detect: { ...spec.detect, any_of: splitCommaList(e.target.value) },
                })
              }
              className="w-72"
            />
          </LabeledInput>
          <LabeledInput label="Confidence score (0-1)" error={fieldError(errors, "detect.score")}>
            <Input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={spec.detect.score}
              onChange={(e) =>
                onChange({ ...spec, detect: { ...spec.detect, score: Number(e.target.value) } })
              }
              className="w-24"
            />
          </LabeledInput>
        </div>
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">Columns</h3>
        <p className="text-xs text-muted-foreground">
          Every column's left/right x-band (PDF points). Requires a `posted_date` and a `description` column, plus at
          least one money column.
        </p>
        <ColumnsEditor
          columns={spec.columns}
          onChange={(columns) => onChange({ ...spec, columns })}
          errors={errors}
          activeColumnIndex={activeColumnIndex}
          onSetActiveColumn={onSetActiveColumn}
        />
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">Sections</h3>
        <SectionsEditor
          sections={spec.sections ?? {}}
          onChange={(sections) => onChange({ ...spec, sections })}
          errors={errors}
        />
      </section>

      <section className="flex flex-wrap gap-3">
        <LabeledInput label="Sign — outflow rows print as">
          <select
            value={spec.sign?.outflow ?? "unsigned"}
            onChange={(e) => onChange({ ...spec, sign: { ...spec.sign, outflow: e.target.value as never } })}
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
          >
            {SIGN_CONVENTIONS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </LabeledInput>
        <LabeledInput label="Sign — inflow rows print as">
          <select
            value={spec.sign?.inflow ?? "leading_minus"}
            onChange={(e) => onChange({ ...spec, sign: { ...spec.sign, inflow: e.target.value as never } })}
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
          >
            {SIGN_CONVENTIONS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </LabeledInput>
        <LabeledInput label="Year inference">
          <select
            value={spec.year_inference ?? "from_period"}
            onChange={(e) => onChange({ ...spec, year_inference: e.target.value as never })}
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
          >
            {YEAR_INFERENCES.map((y) => (
              <option key={y} value={y}>
                {y}
              </option>
            ))}
          </select>
        </LabeledInput>
        <label className="flex items-center gap-2 self-end text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={spec.totals?.section_totals ?? false}
            onChange={(e) => onChange({ ...spec, totals: { section_totals: e.target.checked } })}
          />
          Reconcile per-section totals, when the statement prints them
        </label>
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">Account mask (optional, local-only — I1b)</h3>
        <LabeledInput
          label="Regex with a capture group around the last 4 digits, e.g. 'ending in (\\d{4})'"
          error={fieldError(errors, "account_mask.pattern")}
        >
          <Input
            value={spec.account_mask?.pattern ?? ""}
            onChange={(e) =>
              onChange({ ...spec, account_mask: e.target.value ? { pattern: e.target.value } : undefined })
            }
            className="w-96"
          />
        </LabeledInput>
      </section>

      {errors.length > 0 ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3">
          <p className="text-xs font-medium text-destructive">{errors.length} problem(s) with this spec:</p>
          <ul className="mt-1 list-inside list-disc text-xs text-destructive">
            {errors
              .filter((e) => !e.field.startsWith("columns") && !e.field.startsWith("sections"))
              .map((e) => (
                <li key={e.field + e.message}>
                  {e.field}: {e.message}
                </li>
              ))}
          </ul>
        </div>
      ) : (
        <p className="text-xs font-medium text-emerald-600 dark:text-emerald-400">Valid — no problems found.</p>
      )}
    </div>
  );
}

function splitCommaList(text: string): string[] {
  return text
    .split(",")
    .map((t) => t.trim())
    .filter((t) => t.length > 0);
}

function fieldError(errors: SpecFieldError[], field: string): string | undefined {
  return errors.find((e) => e.field === field)?.message;
}

function LabeledInput({
  label,
  error,
  children,
}: {
  label: string;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1 text-xs text-muted-foreground">
      {label}
      {children}
      {error ? <span className="text-destructive">{error}</span> : null}
    </label>
  );
}
