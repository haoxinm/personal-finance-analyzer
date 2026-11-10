import {
  Toast,
  ToastAction,
  ToastClose,
  ToastDescription,
  ToastProvider,
  ToastTitle,
  ToastViewport,
} from "@/components/ui/toast";
import { useToast } from "@/components/ui/use-toast";

/** Renders the toast queue. Mount once near the root, inside `ToastContextProvider`. */
export function Toaster() {
  const { toasts, dismiss } = useToast();

  return (
    <ToastProvider>
      {toasts.map(({ id, title, description, variant, duration, action }) => (
        <Toast
          key={id}
          variant={variant}
          duration={duration ?? 5000}
          onOpenChange={(open) => {
            if (!open) dismiss(id);
          }}
        >
          <div className="grid gap-1">
            {title ? <ToastTitle>{title}</ToastTitle> : null}
            {description ? <ToastDescription>{description}</ToastDescription> : null}
            {action ? (
              <ToastAction altText={action.label} onClick={action.onClick}>
                {action.label}
              </ToastAction>
            ) : null}
          </div>
          <ToastClose />
        </Toast>
      ))}
      <ToastViewport />
    </ToastProvider>
  );
}
