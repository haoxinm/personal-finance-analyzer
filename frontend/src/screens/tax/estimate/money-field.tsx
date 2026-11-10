import * as React from "react";

import { Input } from "@/components/ui/input";

/** A dollar amount input with a "$" prefix, a help line and an inline validation message. The
 * value stays a string as typed; the form converts it to cents on save. */
export function MoneyField({
  label,
  help,
  value,
  onChange,
  error,
}: {
  label: string;
  help?: string;
  value: string;
  onChange: (value: string) => void;
  error?: string | null;
}) {
  const id = React.useId();
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-medium text-foreground">
        {label}
      </label>
      <div className="relative">
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-sm text-muted-foreground"
        >
          $
        </span>
        <Input
          id={id}
          inputMode="decimal"
          autoComplete="off"
          placeholder="0.00"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          aria-invalid={error ? true : undefined}
          aria-describedby={[help ? helpId : null, error ? errorId : null].filter(Boolean).join(" ") || undefined}
          className="h-8 pl-7 tabular-nums"
        />
      </div>
      {error ? (
        <span id={errorId} role="alert" className="text-xs text-destructive">
          {error}
        </span>
      ) : null}
      {help ? (
        <span id={helpId} className="text-xs text-muted-foreground">
          {help}
        </span>
      ) : null}
    </div>
  );
}
