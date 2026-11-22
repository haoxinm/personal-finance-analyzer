import * as React from "react";
import { createPortal } from "react-dom";

import { cn } from "@/utils";

const FOCUSABLE =
  "select:not([disabled]), textarea:not([disabled]), input:not([disabled]), button:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])";

export interface DialogProps {
  onClose: () => void;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  /** Extra classes for the panel (e.g. a different `max-w-*`). */
  className?: string;
  children: React.ReactNode;
}

/**
 * A modal dialog: portalled to `document.body` above the sticky app header (`z-50`), page scroll
 * locked while open, Escape closes, Tab is trapped, and focus returns to the opener on unmount.
 *
 * Layout contract: the panel is capped at the viewport height. `DialogHeader` and `DialogFooter`
 * never shrink, so the footer's buttons are always visible; `DialogBody` takes the remaining
 * space and scrolls. The overlay itself also scrolls, as a last resort on tiny viewports.
 */
export function Dialog({
  onClose,
  className,
  children,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
}: DialogProps) {
  const panelRef = React.useRef<HTMLDivElement>(null);
  const onCloseRef = React.useRef(onClose);
  onCloseRef.current = onClose;
  const pressedOnOverlay = React.useRef(false);

  // Lock page scroll while open; restore whatever was there before.
  React.useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  // Focus management: move in on mount, go back to the opener on unmount.
  React.useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const panel = panelRef.current;
    const target =
      panel?.querySelector<HTMLElement>("[data-autofocus]") ??
      panel?.querySelector<HTMLElement>(
        "[data-dialog-body] select, [data-dialog-body] textarea, [data-dialog-body] input, [data-dialog-body] button",
      ) ??
      panel?.querySelector<HTMLElement>(FOCUSABLE) ??
      panel;
    target?.focus();
    return () => opener?.focus();
  }, []);

  // Escape and the Tab trap listen on the document, so they work even if focus drifted outside.
  React.useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const panel = panelRef.current;
      if (!panel) return;
      if (e.key === "Escape") {
        e.preventDefault();
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) {
        e.preventDefault();
        return;
      }
      const active = document.activeElement;
      const inside = active instanceof Node && panel.contains(active);
      if (e.shiftKey && (active === first || !inside)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || !inside)) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  return createPortal(
    <div
      data-testid="dialog-overlay"
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/50 p-4 sm:items-center"
      // mousedown + mouseup on the overlay itself (not click): a text-selection drag that ends
      // outside the panel must not close the dialog.
      onMouseDown={(e) => {
        pressedOnOverlay.current = e.target === e.currentTarget;
      }}
      onMouseUp={(e) => {
        if (pressedOnOverlay.current && e.target === e.currentTarget) onClose();
        pressedOnOverlay.current = false;
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledBy}
        tabIndex={-1}
        className={cn(
          "relative flex max-h-[calc(100dvh-2rem)] w-full max-w-lg flex-col rounded-lg border border-border bg-background text-foreground shadow-lg outline-none",
          className,
        )}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}

export function DialogHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("flex shrink-0 flex-col gap-1 border-b border-border p-4 [overflow-wrap:anywhere]", className)}
      {...props}
    />
  );
}

export function DialogTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn("text-base font-semibold leading-tight", className)} {...props} />;
}

export function DialogDescription({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("text-sm text-muted-foreground", className)} {...props} />;
}

/** The scrolling region. Long unbroken text wraps instead of widening the panel. */
export function DialogBody({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      data-dialog-body=""
      className={cn(
        "flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4 [overflow-wrap:anywhere]",
        className,
      )}
      {...props}
    />
  );
}

export function DialogFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("flex shrink-0 flex-wrap justify-end gap-2 border-t border-border p-4", className)}
      {...props}
    />
  );
}
