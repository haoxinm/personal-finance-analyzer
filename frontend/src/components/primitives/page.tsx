import * as React from "react";

import { cn } from "@/utils";

/**
 * The root of every screen: one column with the shared vertical rhythm. The shell's `<main>`
 * already pads the page, so a screen adds no padding of its own.
 */
export function Page({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("flex flex-col gap-6", className)}>{children}</div>;
}

/**
 * A screen's one `<h1>` with its optional description and page-level actions (export buttons, a
 * currency picker, ...). The title names what the screen is for and can be longer than its nav
 * label ("Import statements" on the Import tab). Which user is being viewed is the user bar's job,
 * not repeated in the description.
 */
export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: string;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <header className={cn("flex flex-wrap items-start justify-between gap-3", className)}>
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description ? <p className="text-sm text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}
