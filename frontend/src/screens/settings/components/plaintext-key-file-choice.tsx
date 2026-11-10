import * as React from "react";

import { Button } from "@/components/ui/button";

import type { FileKeyAction } from "../lib/hooks";
import type { SecretStorageStatus } from "../lib/secret-storage";

interface PlaintextKeyFileChoiceProps {
  status: SecretStorageStatus;
  pending: boolean;
  onApply: (action: FileKeyAction) => void;
  onCancel: () => void;
}

/**
 * What to do with an API key that is saved in the plaintext key file. Turning the file off never
 * silently leaves the key on disk: with a working keychain the key can move into it; without one the
 * user has to choose between deleting the file (the default, behind an explicit button) and keeping
 * it on disk, unused.
 */
export function PlaintextKeyFileChoice({ status, pending, onApply, onCancel }: PlaintextKeyFileChoiceProps) {
  const canMove = status.keychain_usable;
  const [action, setAction] = React.useState<FileKeyAction>(canMove ? "move" : "delete");

  const options: { value: FileKeyAction; label: string; help: string }[] = [
    ...(canMove
      ? [
          {
            value: "move" as const,
            label: `Move it into ${status.keychain_name}`,
            help: "The key stays available; the plaintext file is overwritten and removed.",
          },
        ]
      : []),
    {
      value: "delete",
      label: "Delete the key file",
      help: "Overwritten, then removed. The key is no longer stored on this computer; you would enter it again to use an LLM.",
    },
    {
      value: "keep",
      label: "Keep the file on disk",
      help: "The key stays as plaintext in the file, unused while the plaintext key file is off. You can delete it later.",
    },
  ];
  const confirmLabel = {
    move: `Move into ${status.keychain_name}`,
    delete: "Delete the key file",
    keep: "Keep the file",
  }[action];

  return (
    <div
      role="group"
      aria-label="What to do with the saved key"
      data-testid="plaintext-key-choice"
      className="mt-2 flex flex-col gap-2 rounded-md border border-border bg-background p-3 text-xs"
    >
      <p className="font-medium">A key is saved as plaintext in {status.file_store_path}. What should happen to it?</p>
      {options.map((option) => (
        <label key={option.value} className="flex items-start gap-2">
          <input
            type="radio"
            name="file-key-action"
            className="mt-0.5"
            checked={action === option.value}
            onChange={() => setAction(option.value)}
          />
          <span>
            {option.label}
            <span className="block text-muted-foreground">{option.help}</span>
          </span>
        </label>
      ))}
      <div className="flex gap-2">
        <Button
          size="sm"
          variant={action === "delete" ? "destructive" : "default"}
          disabled={pending}
          onClick={() => onApply(action)}
        >
          {confirmLabel}
        </Button>
        <Button size="sm" variant="outline" disabled={pending} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
