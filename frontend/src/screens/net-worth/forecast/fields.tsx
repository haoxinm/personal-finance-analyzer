import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/utils";

import { formatDollarsInput, parseDollars } from "./params";

const LABEL = "text-xs font-medium text-muted-foreground";

/** "Data: $85,000 (2024)" with a button that puts the data-derived value back. */
function DataHint({
  text,
  canReset,
  onReset,
  resetLabel,
}: {
  text: string | null;
  canReset: boolean;
  onReset?: () => void;
  resetLabel: string;
}) {
  if (text === null) return null;
  return (
    <p className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
      <span>{text}</span>
      {canReset && onReset ? (
        <Button type="button" variant="link" size="sm" className="h-auto p-0 text-xs" onClick={onReset} aria-label={resetLabel}>
          Reset to data
        </Button>
      ) : null}
    </p>
  );
}

export interface MoneyFieldProps {
  label: string;
  /** Minor units; `null` = "use the data default" (only when `nullable`). */
  value: number | null;
  onChange: (value: number | null) => void;
  /** Empty input means `null` (use data) rather than 0. */
  nullable?: boolean;
  allowNegative?: boolean;
  /** Shown as the placeholder when empty. */
  placeholder?: string;
  /** "Data: ..." line; `null` hides it. */
  dataText?: string | null;
  onReset?: () => void;
  disabled?: boolean;
  /** Visually hide the label (a table cell), keeping it for screen readers. */
  hideLabel?: boolean;
  className?: string;
}

/** A dollar amount input (with a "$" prefix): the user types dollars, the value is cents. Keeps
 * what is being typed while focused, so "-" or "1." are not rewritten mid-edit. */
export function MoneyField({
  label,
  value,
  onChange,
  nullable = false,
  allowNegative = false,
  placeholder,
  dataText = null,
  onReset,
  disabled,
  hideLabel,
  className,
}: MoneyFieldProps) {
  const id = React.useId();
  const [draft, setDraft] = React.useState(value === null ? "" : formatDollarsInput(value));
  const [focused, setFocused] = React.useState(false);
  const [invalid, setInvalid] = React.useState(false);
  React.useEffect(() => {
    if (!focused) {
      setDraft(value === null ? "" : formatDollarsInput(value));
      setInvalid(false);
    }
  }, [value, focused]);

  function onType(text: string) {
    setDraft(text);
    if (text.trim() === "") {
      setInvalid(false);
      onChange(nullable ? null : 0);
      return;
    }
    const parsed = parseDollars(text, allowNegative);
    setInvalid(parsed === null);
    if (parsed !== null) onChange(parsed);
  }

  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <label htmlFor={id} className={hideLabel ? "sr-only" : LABEL}>
        {label}
      </label>
      <div className="relative">
        <span aria-hidden="true" className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-sm text-muted-foreground">
          $
        </span>
        <Input
          id={id}
          inputMode="decimal"
          autoComplete="off"
          className="pl-6"
          value={draft}
          placeholder={placeholder ?? "0.00"}
          disabled={disabled}
          onChange={(e) => onType(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          aria-invalid={invalid || undefined}
          aria-describedby={invalid ? `${id}-error` : undefined}
        />
      </div>
      {invalid ? (
        <p id={`${id}-error`} role="alert" className="text-xs text-destructive">
          Enter {allowNegative ? "an" : "a non-negative"} amount, like 1,234.56.
        </p>
      ) : null}
      <DataHint text={dataText} canReset={value !== null} onReset={onReset} resetLabel={`Reset ${label} to data`} />
    </div>
  );
}

export interface PercentFieldProps {
  label: string;
  value: number | null;
  onChange: (value: number | null) => void;
  /** Accepted range (the API's bounds); a value outside it is flagged and not applied. */
  min: number;
  max: number;
  step?: number;
  nullable?: boolean;
  /** A range slider next to the number input, over `[sliderMin, sliderMax]`. */
  slider?: { min: number; max: number };
  placeholder?: string;
  dataText?: string | null;
  /** Whether "Reset to data" does anything (value differs from the data-derived one). */
  canReset?: boolean;
  onReset?: () => void;
  className?: string;
}

/** A percent-per-year input with a "%" suffix and optional slider. */
export function PercentField({
  label,
  value,
  onChange,
  min,
  max,
  step = 0.1,
  nullable = false,
  slider,
  placeholder,
  dataText = null,
  canReset,
  onReset,
  className,
}: PercentFieldProps) {
  const id = React.useId();
  const [draft, setDraft] = React.useState(value === null ? "" : String(value));
  const [focused, setFocused] = React.useState(false);
  const [invalid, setInvalid] = React.useState(false);
  React.useEffect(() => {
    if (!focused) {
      setDraft(value === null ? "" : String(value));
      setInvalid(false);
    }
  }, [value, focused]);

  function onType(text: string) {
    setDraft(text);
    if (text.trim() === "") {
      setInvalid(!nullable);
      if (nullable) onChange(null);
      return;
    }
    const n = Number(text);
    const ok = Number.isFinite(n) && n >= min && n <= max;
    setInvalid(!ok);
    if (ok) onChange(n);
  }

  const sliderValue = Math.min(Math.max(value ?? 0, slider?.min ?? min), slider?.max ?? max);
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <label htmlFor={id} className={LABEL}>
        {label}
      </label>
      <div className="flex items-center gap-3">
        <div className="relative w-28 shrink-0">
          <Input
            id={id}
            type="number"
            inputMode="decimal"
            step={step}
            min={min}
            max={max}
            className="pr-7"
            value={draft}
            placeholder={placeholder}
            onChange={(e) => onType(e.target.value)}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            aria-invalid={invalid || undefined}
            aria-describedby={invalid ? `${id}-error` : undefined}
          />
          <span aria-hidden="true" className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-sm text-muted-foreground">
            %
          </span>
        </div>
        {slider ? (
          <input
            type="range"
            aria-label={`${label} slider`}
            className="h-2 min-w-0 flex-1 accent-primary"
            min={slider.min}
            max={slider.max}
            step={step}
            value={sliderValue}
            onChange={(e) => onChange(Number(e.target.value))}
          />
        ) : null}
      </div>
      {invalid ? (
        <p id={`${id}-error`} role="alert" className="text-xs text-destructive">
          {nullable ? `Enter a number from ${min} to ${max}, or leave empty.` : `Enter a number from ${min} to ${max}.`}
        </p>
      ) : null}
      <DataHint text={dataText} canReset={canReset ?? value !== null} onReset={onReset} resetLabel={`Reset ${label} to data`} />
    </div>
  );
}

export interface YearFieldProps {
  label: string;
  value: number | null;
  onChange: (value: number | null) => void;
  min: number;
  max: number;
  /** Empty input means `null`. */
  nullable?: boolean;
  placeholder?: string;
  hint?: string;
  className?: string;
}

/** A calendar-year input; an out-of-range year is flagged and not applied. */
export function YearField({ label, value, onChange, min, max, nullable = false, placeholder, hint, className }: YearFieldProps) {
  const id = React.useId();
  const [draft, setDraft] = React.useState(value === null ? "" : String(value));
  const [focused, setFocused] = React.useState(false);
  const [invalid, setInvalid] = React.useState(false);
  React.useEffect(() => {
    if (!focused) {
      setDraft(value === null ? "" : String(value));
      setInvalid(false);
    }
  }, [value, focused]);

  function onType(text: string) {
    setDraft(text);
    if (text.trim() === "") {
      setInvalid(!nullable);
      if (nullable) onChange(null);
      return;
    }
    const n = Number(text);
    const ok = Number.isInteger(n) && n >= min && n <= max;
    setInvalid(!ok);
    if (ok) onChange(n);
  }

  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <label htmlFor={id} className={LABEL}>
        {label}
      </label>
      <Input
        id={id}
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        value={draft}
        placeholder={placeholder}
        onChange={(e) => onType(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        aria-invalid={invalid || undefined}
        aria-describedby={invalid ? `${id}-error` : hint ? `${id}-hint` : undefined}
      />
      {invalid ? (
        <p id={`${id}-error`} role="alert" className="text-xs text-destructive">
          Enter a year from {min} to {max}
          {nullable ? ", or leave empty" : ""}.
        </p>
      ) : hint ? (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      ) : null}
    </div>
  );
}
