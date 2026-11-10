import * as ToastPrimitive from "@radix-ui/react-toast";
import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/utils";

export const ToastProvider = ToastPrimitive.Provider;

export const ToastViewport = React.forwardRef<
  React.ElementRef<typeof ToastPrimitive.Viewport>,
  React.ComponentPropsWithoutRef<typeof ToastPrimitive.Viewport>
>(({ className, ...props }, ref) => (
  <ToastPrimitive.Viewport
    ref={ref}
    className={cn(
      "fixed bottom-0 right-0 z-[100] flex max-h-screen w-full flex-col gap-2 p-4 sm:max-w-[380px]",
      className,
    )}
    {...props}
  />
));
ToastViewport.displayName = ToastPrimitive.Viewport.displayName;

const toastVariants = cva(
  "group pointer-events-auto relative flex w-full items-center justify-between gap-3 overflow-hidden " +
    "rounded-md border p-4 shadow-lg transition-all",
  {
    variants: {
      variant: {
        default: "border-border bg-card text-card-foreground",
        destructive: "border-destructive bg-destructive text-destructive-foreground",
        success: "border-inflow bg-card text-card-foreground",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export const Toast = React.forwardRef<
  React.ElementRef<typeof ToastPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof ToastPrimitive.Root> & VariantProps<typeof toastVariants>
>(({ className, variant, ...props }, ref) => (
  <ToastPrimitive.Root ref={ref} className={cn(toastVariants({ variant }), className)} {...props} />
));
Toast.displayName = ToastPrimitive.Root.displayName;

export const ToastTitle = React.forwardRef<
  React.ElementRef<typeof ToastPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof ToastPrimitive.Title>
>(({ className, ...props }, ref) => (
  <ToastPrimitive.Title ref={ref} className={cn("text-sm font-semibold", className)} {...props} />
));
ToastTitle.displayName = ToastPrimitive.Title.displayName;

export const ToastDescription = React.forwardRef<
  React.ElementRef<typeof ToastPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof ToastPrimitive.Description>
>(({ className, ...props }, ref) => (
  <ToastPrimitive.Description ref={ref} className={cn("text-sm opacity-90", className)} {...props} />
));
ToastDescription.displayName = ToastPrimitive.Description.displayName;

export const ToastClose = React.forwardRef<
  React.ElementRef<typeof ToastPrimitive.Close>,
  React.ComponentPropsWithoutRef<typeof ToastPrimitive.Close>
>(({ className, ...props }, ref) => (
  <ToastPrimitive.Close
    ref={ref}
    aria-label="Dismiss"
    className={cn(
      "absolute right-2 top-2 rounded-md p-1 text-foreground/50 opacity-0 transition-opacity " +
        "hover:text-foreground focus:opacity-100 focus:outline-none group-hover:opacity-100",
      className,
    )}
    {...props}
  >
    <span aria-hidden="true">&times;</span>
  </ToastPrimitive.Close>
));
ToastClose.displayName = ToastPrimitive.Close.displayName;

export const ToastAction = React.forwardRef<
  React.ElementRef<typeof ToastPrimitive.Action>,
  React.ComponentPropsWithoutRef<typeof ToastPrimitive.Action>
>(({ className, ...props }, ref) => (
  <ToastPrimitive.Action
    ref={ref}
    className={cn(
      "mt-1 inline-flex h-8 shrink-0 items-center justify-center rounded-md border border-border " +
        "bg-transparent px-3 text-xs font-medium hover:bg-muted focus:outline-none focus:ring-1 focus:ring-ring",
      className,
    )}
    {...props}
  />
));
ToastAction.displayName = ToastPrimitive.Action.displayName;

export type ToastActionElement = React.ReactElement<typeof ToastPrimitive.Action>;
