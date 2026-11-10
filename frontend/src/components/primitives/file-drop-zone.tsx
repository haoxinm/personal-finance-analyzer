import * as React from "react";

import { cn } from "@/utils";

export interface FileDropZoneProps {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
  /** Overrides for the tax-form importer; defaults keep the statement wording. */
  title?: string;
  hint?: string;
  inputLabel?: string;
}

/** Drag-and-drop (or click-to-browse) multiple PDFs (statements, tax forms). Filters to
 * `.pdf`/`application/pdf` client-side as a friendly first pass — the server re-checks the
 * magic bytes regardless (A12), so this is UX only, never the security boundary. */
export function FileDropZone({
  onFiles,
  disabled = false,
  title = "Drag statements here, or click to browse",
  hint = "Native digital PDFs only — scanned or photographed statements can\u2019t be read.",
  inputLabel = "Choose statement PDFs",
}: FileDropZoneProps) {
  const [isDraggingOver, setIsDraggingOver] = React.useState(false);
  const inputRef = React.useRef<HTMLInputElement>(null);

  const acceptFiles = React.useCallback(
    (fileList: FileList | null) => {
      if (!fileList) return;
      const pdfs = Array.from(fileList).filter(
        (file) => file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf"),
      );
      if (pdfs.length > 0) onFiles(pdfs);
    },
    [onFiles],
  );

  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled}
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-10 text-center transition-colors",
        disabled
          ? "cursor-not-allowed border-border bg-muted/30 text-muted-foreground"
          : "cursor-pointer border-border hover:border-accent-foreground/40 hover:bg-muted/40",
        isDraggingOver && !disabled && "border-accent-foreground bg-accent/40",
      )}
      onClick={() => {
        if (!disabled) inputRef.current?.click();
      }}
      onKeyDown={(event) => {
        if (!disabled && (event.key === "Enter" || event.key === " ")) {
          event.preventDefault();
          inputRef.current?.click();
        }
      }}
      onDragOver={(event) => {
        if (disabled) return;
        event.preventDefault();
        setIsDraggingOver(true);
      }}
      onDragLeave={() => setIsDraggingOver(false)}
      onDrop={(event) => {
        event.preventDefault();
        setIsDraggingOver(false);
        if (!disabled) acceptFiles(event.dataTransfer.files);
      }}
    >
      <p className="text-sm font-medium text-foreground">{title}</p>
      <p className="text-xs text-muted-foreground">{hint}</p>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf,.pdf"
        multiple
        disabled={disabled}
        className="hidden"
        onChange={(event) => {
          acceptFiles(event.target.files);
          event.target.value = "";
        }}
        aria-label={inputLabel}
      />
    </div>
  );
}
