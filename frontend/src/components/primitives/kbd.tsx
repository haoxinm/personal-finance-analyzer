import * as React from "react";

import { cn } from "@/utils";

/**
 * A visible keyboard-shortcut hint inside a button label. Decorative for assistive tech
 * (`aria-hidden`): the button keeps a plain accessible name and announces the key through
 * `aria-keyshortcuts` instead.
 */
export function Kbd({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <kbd
      aria-hidden="true"
      className={cn("ml-2 rounded border border-current px-1.5 opacity-80 font-mono text-xs font-normal leading-5", className)}
    >
      {children}
    </kbd>
  );
}
