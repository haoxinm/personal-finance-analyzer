import * as React from "react";

import { ToastContext, type ToastInput } from "@/components/ui/toast-context";

/** `toast()` that does nothing when no provider is mounted (isolated component tests); the app
 * always mounts one (`App.tsx`). */
export function useSafeToast(): (input: ToastInput) => string | undefined {
  const ctx = React.useContext(ToastContext);
  return React.useCallback((input: ToastInput) => ctx?.toast(input), [ctx]);
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong.";
}
