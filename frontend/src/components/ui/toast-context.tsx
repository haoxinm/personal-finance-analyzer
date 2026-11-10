import * as React from "react";

export interface ToastRecord {
  id: string;
  title?: React.ReactNode;
  description?: React.ReactNode;
  variant?: "default" | "destructive" | "success";
  duration?: number;
  /** An inline button (e.g. "Undo"); clicking it also dismisses the toast. */
  action?: { label: string; onClick: () => void };
}

export type ToastInput = Omit<ToastRecord, "id">;

export interface ToastContextValue {
  toasts: ToastRecord[];
  toast: (input: ToastInput) => string;
  dismiss: (id: string) => void;
}

export const ToastContext = React.createContext<ToastContextValue | null>(null);
