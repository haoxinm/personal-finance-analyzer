import * as React from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

import type { Settings, WatchSettings } from "../lib/hooks";

export const MIN_INTERVAL_SECONDS = 10;
export const MAX_INTERVAL_SECONDS = 86400;

export interface FolderImportsSectionProps {
  settings: Settings;
  onSave: (watch: WatchSettings) => void;
  saving: boolean;
}

const DEFAULT_WATCH: WatchSettings = { enabled: false, interval_seconds: 60, watched_folder: "" };

/** Folder imports: import PDFs put into the documents folder by hand (off by default). The folder
 * itself is shown read-only: where it is is chosen by whoever runs the app (`PFA_DOCUMENTS_DIR`). */
export function FolderImportsSection({ settings, onSave, saving }: FolderImportsSectionProps) {
  const saved = settings.watch ?? DEFAULT_WATCH;
  const [enabled, setEnabled] = React.useState(saved.enabled);
  const [intervalText, setIntervalText] = React.useState(String(saved.interval_seconds));
  React.useEffect(() => {
    setEnabled(saved.enabled);
    setIntervalText(String(saved.interval_seconds));
  }, [saved.enabled, saved.interval_seconds]);

  const parsed = Number(intervalText);
  const intervalValid =
    intervalText.trim() !== "" && Number.isInteger(parsed) && parsed >= MIN_INTERVAL_SECONDS && parsed <= MAX_INTERVAL_SECONDS;
  const dirty = enabled !== saved.enabled || (intervalValid && parsed !== saved.interval_seconds);

  const controlled = (settings.env_overrides ?? []).filter((name) => name.startsWith("watch."));
  const envControlled = controlled.length > 0;
  const llmOn = settings.llm.mode !== "none";

  return (
    <Card data-testid="folder-imports-section">
      <CardHeader>
        <CardTitle>Folder imports</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="text-xs text-muted-foreground">
          Import PDFs you put into the documents folder yourself, for example from another computer over a network
          share. The folders say whose a file is and what it is:{" "}
          <span className="font-mono">&lt;Person&gt;/Statements/…</span> is a statement and{" "}
          <span className="font-mono">&lt;Person&gt;/Tax/&lt;year&gt;/…</span> a tax form for that person. The person
          folder must match the name of a person in the app (capitals and spacing do not matter); an unknown name is
          reported, never turned into a person. Files stay exactly where you put them.
        </p>

        <div className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Documents folder (watched)</span>
          <code className="break-all rounded bg-muted px-2 py-1 text-xs" data-testid="folder-imports-folder">
            {saved.watched_folder || "unknown"}
          </code>
          <span className="text-xs text-muted-foreground">
            Set with <span className="font-mono">PFA_DOCUMENTS_DIR</span> or <span className="font-mono">[storage]
            documents_dir</span> in config.toml; not changeable here.
          </span>
        </div>

        <div className="flex flex-col gap-2">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={enabled}
              disabled={controlled.includes("watch.enabled")}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            Import new PDFs from the documents folder automatically
          </label>
          <p className="text-xs text-muted-foreground">
            Off by default. When on, the server looks at the folder every few seconds, imports a new PDF once it has
            stopped changing (a few seconds after the copy finishes, within one to two intervals), and runs it through
            the same import and categorization as an upload, with no click.{" "}
            {llmOn ? (
              <strong className="text-foreground">
                Your LLM is enabled, so the descriptions of new statements are sent to it to be categorized
                automatically, exactly as after an upload; nothing asks first.
              </strong>
            ) : (
              <>If you enable an LLM later, new statements&apos; descriptions will be sent to it automatically too.</>
            )}{" "}
            Tax forms are read locally unless you also turned on the tax LLM fallback.
          </p>
        </div>

        <label className="flex max-w-xs flex-col gap-1 text-sm">
          Check the folder every (seconds)
          <Input
            type="number"
            min={MIN_INTERVAL_SECONDS}
            max={MAX_INTERVAL_SECONDS}
            step={1}
            value={intervalText}
            disabled={controlled.includes("watch.interval_seconds")}
            onChange={(e) => setIntervalText(e.target.value)}
            aria-invalid={!intervalValid}
            aria-describedby="folder-imports-interval-help"
          />
          <span
            id="folder-imports-interval-help"
            className={intervalValid ? "text-xs text-muted-foreground" : "text-xs text-destructive"}
          >
            {MIN_INTERVAL_SECONDS} to {MAX_INTERVAL_SECONDS} seconds (default 60).
          </span>
        </label>

        {envControlled ? (
          <p className="text-xs text-muted-foreground" data-testid="folder-imports-env">
            Controlled by an environment variable ({controlled.join(", ")}); change it where the app is started.
          </p>
        ) : null}

        <div>
          <Button
            onClick={() => onSave({ enabled, interval_seconds: parsed, watched_folder: saved.watched_folder })}
            disabled={!dirty || saving || !intervalValid}
          >
            Save folder imports
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
