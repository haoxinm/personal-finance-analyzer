import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/use-toast";

import { useCreatePreset, useDeletePreset, useUpdatePreset } from "./api";
import {
  isModified,
  toApiParams,
  type BuiltinPreset,
  type ForecastState,
  type PresetRef,
  type Resolved,
  type SavedPreset,
} from "./params";

export interface PresetBarProps {
  /** Selected user, or null for everyone (presets then save as shared). */
  userId: number | null;
  builtin: readonly BuiltinPreset[];
  saved: readonly SavedPreset[];
  selected: PresetRef | null;
  state: ForecastState;
  resolved: Pick<Resolved, "baseYear" | "targetYear">;
  onSelect: (ref: PresetRef | null) => void;
  /** A saved preset was just created: select it without re-applying it over the scenario. */
  onSaved: (preset: SavedPreset) => void;
}

const SELECT =
  "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

/** The preset picker (built-ins and saved) with save, update, rename and delete for saved ones,
 * and a "modified" mark once the scenario drifts from the chosen preset. */
export function PresetBar({ userId, builtin, saved, selected, state, resolved, onSelect, onSaved }: PresetBarProps) {
  const uid = React.useId();
  const { toast } = useToast();
  const create = useCreatePreset();
  const update = useUpdatePreset();
  const remove = useDeletePreset();
  const [mode, setMode] = React.useState<"idle" | "save" | "rename" | "delete">("idle");
  const [name, setName] = React.useState("");
  const [shared, setShared] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const current: BuiltinPreset | SavedPreset | null =
    selected === null
      ? null
      : selected.kind === "builtin"
        ? (builtin.find((p) => p.key === selected.key) ?? null)
        : (saved.find((p) => p.id === selected.id) ?? null);
  const savedCurrent = current !== null && "id" in current ? current : null;
  const modified = current !== null && isModified(state, resolved, current);
  const value = current === null ? "" : "key" in current ? `builtin:${current.key}` : `saved:${current.id}`;

  function close() {
    setMode("idle");
    setName("");
    setError(null);
  }

  function fail(title: string, err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    setError(message);
    toast({ variant: "destructive", title, description: message });
  }

  function onPick(next: string) {
    close();
    if (next === "") onSelect(null);
    else if (next.startsWith("builtin:")) onSelect({ kind: "builtin", key: next.slice("builtin:".length) });
    else onSelect({ kind: "saved", id: Number(next.slice("saved:".length)) });
  }

  function submitSave(event: React.FormEvent) {
    event.preventDefault();
    if (!name.trim()) {
      setError("Enter a name for the preset.");
      return;
    }
    create.mutate(
      { name: name.trim(), userId: userId !== null && !shared ? userId : null, params: toApiParams(state, resolved) },
      {
        onSuccess: (preset) => {
          toast({ variant: "success", title: "Preset saved", description: preset.name });
          onSaved(preset);
          close();
        },
        onError: (err) => fail("Could not save preset", err),
      },
    );
  }

  function submitRename(event: React.FormEvent) {
    event.preventDefault();
    if (!savedCurrent) return;
    if (!name.trim()) {
      setError("Enter a name for the preset.");
      return;
    }
    update.mutate(
      { id: savedCurrent.id, name: name.trim() },
      {
        onSuccess: (preset) => {
          toast({ variant: "success", title: "Preset renamed", description: preset.name });
          close();
        },
        onError: (err) => fail("Could not rename preset", err),
      },
    );
  }

  function overwrite() {
    if (!savedCurrent) return;
    update.mutate(
      { id: savedCurrent.id, params: toApiParams(state, resolved) },
      {
        onSuccess: (preset) => toast({ variant: "success", title: "Preset updated", description: preset.name }),
        onError: (err) => fail("Could not update preset", err),
      },
    );
  }

  function confirmDelete() {
    if (!savedCurrent) return;
    remove.mutate(savedCurrent.id, {
      onSuccess: () => {
        toast({ variant: "success", title: "Preset deleted", description: savedCurrent.name });
        onSelect(null);
        close();
      },
      onError: (err) => fail("Could not delete preset", err),
    });
  }

  return (
    <div className="flex flex-col gap-3" data-testid="preset-bar">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex min-w-48 flex-col gap-1">
          <label htmlFor={`${uid}-preset`} className="text-xs font-medium text-muted-foreground">
            Preset
          </label>
          <select id={`${uid}-preset`} className={SELECT} value={value} onChange={(e) => onPick(e.target.value)}>
            <option value="">Custom</option>
            <optgroup label="Built-in">
              {builtin.map((p) => (
                <option key={p.key} value={`builtin:${p.key}`}>
                  {p.name}
                </option>
              ))}
            </optgroup>
            {saved.length > 0 ? (
              <optgroup label="Saved">
                {saved.map((p) => (
                  <option key={p.id} value={`saved:${p.id}`}>
                    {p.name}
                    {p.user_id === null ? " (shared)" : ""}
                  </option>
                ))}
              </optgroup>
            ) : null}
          </select>
        </div>
        {modified && current ? <Badge variant="accent">Modified from {current.name}</Badge> : null}
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => { setError(null); setName(""); setMode("save"); }}>
            Save as preset…
          </Button>
          {savedCurrent && modified ? (
            <Button type="button" variant="outline" size="sm" disabled={update.isPending} onClick={overwrite}>
              Update “{savedCurrent.name}”
            </Button>
          ) : null}
          {savedCurrent ? (
            <>
              <Button type="button" variant="ghost" size="sm" onClick={() => { setError(null); setName(savedCurrent.name); setMode("rename"); }}>
                Rename
              </Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => setMode("delete")}>
                Delete
              </Button>
            </>
          ) : null}
        </div>
      </div>

      {current && "key" in current ? (
        <p className="text-xs text-muted-foreground">
          {current.description} Built-in presets set only these four rates; they are illustrative, not predictions.
        </p>
      ) : null}

      {mode === "save" || mode === "rename" ? (
        <form
          onSubmit={mode === "save" ? submitSave : submitRename}
          aria-label={mode === "save" ? "Save preset" : "Rename preset"}
          className="flex flex-col gap-2 rounded-md border border-border p-3"
        >
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex min-w-56 flex-1 flex-col gap-1">
              <label htmlFor={`${uid}-name`} className="text-xs font-medium text-muted-foreground">
                Preset name
              </label>
              <Input
                id={`${uid}-name`}
                value={name}
                maxLength={60}
                autoFocus
                onChange={(e) => setName(e.target.value)}
                aria-invalid={error ? true : undefined}
                aria-describedby={error ? `${uid}-name-error` : undefined}
              />
            </div>
            {mode === "save" && userId !== null ? (
              <label className="flex items-center gap-2 pb-2 text-sm">
                <input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} />
                Share with everyone
              </label>
            ) : null}
            <Button type="submit" size="sm" disabled={create.isPending || update.isPending}>
              {mode === "save" ? "Save" : "Rename"}
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={close}>
              Cancel
            </Button>
          </div>
          {mode === "save" && userId === null ? (
            <p className="text-xs text-muted-foreground">Saved for all users (no one is selected).</p>
          ) : null}
          {error ? (
            <p id={`${uid}-name-error`} role="alert" className="text-xs text-destructive">
              {error}
            </p>
          ) : null}
        </form>
      ) : null}

      {mode === "delete" && savedCurrent ? (
        <div role="alertdialog" aria-label="Delete preset" className="flex flex-wrap items-center gap-3 rounded-md border border-destructive/30 p-3 text-sm">
          <span>Delete the preset “{savedCurrent.name}”? This cannot be undone.</span>
          <Button type="button" variant="destructive" size="sm" disabled={remove.isPending} onClick={confirmDelete}>
            Delete preset
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={close}>
            Cancel
          </Button>
        </div>
      ) : null}
    </div>
  );
}
