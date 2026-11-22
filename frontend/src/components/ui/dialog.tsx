import * as React from "react";
import { createPortal } from "react-dom";

import { cn } from "@/utils";

const NOT_SKIPPED = ":not([tabindex='-1']):not([disabled])";
const FOCUSABLE = [
  `select${NOT_SKIPPED}`,
  `textarea${NOT_SKIPPED}`,
  `input:not([type='hidden'])${NOT_SKIPPED}`,
  `button${NOT_SKIPPED}`,
  "a[href]:not([tabindex='-1'])",
  "[tabindex]:not([tabindex='-1']):not([disabled])",
].join(", ");

/** Tab stops inside `root`: no `tabindex="-1"`, disabled, `hidden` or non-rendered elements. */
function tabStops(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => {
    if (el.closest("[hidden], [inert]")) return false;
    const style = window.getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden";
  });
}

/** Per-element hide state shared by every open dialog: stacked dialogs each hide the background,
 * and an element only comes back once the last dialog that hid it has closed. */
const hidden = new Map<Element, { count: number; ariaHidden: string | null; wasInert: boolean }>();

function hide(el: Element): () => void {
  const entry = hidden.get(el);
  if (entry) entry.count += 1;
  else {
    hidden.set(el, { count: 1, ariaHidden: el.getAttribute("aria-hidden"), wasInert: el.hasAttribute("inert") });
    el.setAttribute("inert", "");
    el.setAttribute("aria-hidden", "true");
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    const current = hidden.get(el);
    if (!current) return;
    current.count -= 1;
    if (current.count > 0) return;
    hidden.delete(el);
    if (!current.wasInert) el.removeAttribute("inert");
    if (current.ariaHidden === null) el.removeAttribute("aria-hidden");
    else el.setAttribute("aria-hidden", current.ariaHidden);
  };
}

/** Marks an element (and anything holding a `data-dialog-exempt` element, one level at a time)
 * `inert` + `aria-hidden`, returning an undo function. The dialog's own host and exempt
 * elements (the toast viewport) stay live. Reference-counted so stacked dialogs compose. */
function hideBackground(host: HTMLElement): () => void {
  const undo: Array<() => void> = [];
  const visit = (parent: Element) => {
    for (const child of Array.from(parent.children)) {
      if (child === host || child.hasAttribute("data-dialog-exempt")) continue;
      if (child.querySelector("[data-dialog-exempt]")) {
        visit(child);
        continue;
      }
      if (child.tagName === "SCRIPT") continue;
      undo.push(hide(child));
    }
  };
  visit(document.body);
  return () => undo.forEach((fn) => fn());
}

export interface DialogProps {
  onClose: () => void;
  /** What Escape and a press outside the panel call, when set (default: `onClose`). Lets a form
   * with unsaved changes ask before it closes; `onClose` stays the plain "go away". */
  onRequestClose?: () => void;
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
  onRequestClose,
  className,
  children,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
}: DialogProps) {
  const panelRef = React.useRef<HTMLDivElement>(null);
  const overlayRef = React.useRef<HTMLDivElement>(null);
  const requestCloseRef = React.useRef(onRequestClose ?? onClose);
  requestCloseRef.current = onRequestClose ?? onClose;
  const pressedOnOverlay = React.useRef(false);

  // Lock page scroll while open; restore whatever was there before. The vanishing scrollbar's
  // width is added as padding so the page behind does not jump sideways.
  React.useEffect(() => {
    const { overflow, paddingRight } = document.body.style;
    const scrollbar = Math.max(0, window.innerWidth - document.documentElement.clientWidth);
    document.body.style.overflow = "hidden";
    if (scrollbar > 0) {
      const current = parseFloat(window.getComputedStyle(document.body).paddingRight) || 0;
      document.body.style.paddingRight = `${current + scrollbar}px`;
    }
    return () => {
      document.body.style.overflow = overflow;
      document.body.style.paddingRight = paddingRight;
    };
  }, []);

  // Everything behind the dialog is unreachable for assistive tech and the keyboard.
  React.useEffect(() => {
    const host = overlayRef.current;
    return host ? hideBackground(host) : undefined;
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
        requestCloseRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const focusable = tabStops(panel);
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
      ref={overlayRef}
      data-testid="dialog-overlay"
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto overscroll-contain bg-black/50 p-4 sm:items-center"
      // mousedown + mouseup on the overlay itself (not click): a text-selection drag that ends
      // outside the panel must not close the dialog.
      onMouseDown={(e) => {
        pressedOnOverlay.current = e.target === e.currentTarget;
      }}
      onMouseUp={(e) => {
        if (pressedOnOverlay.current && e.target === e.currentTarget) requestCloseRef.current();
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
