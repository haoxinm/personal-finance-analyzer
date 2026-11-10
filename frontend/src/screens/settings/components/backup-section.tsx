import * as React from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/use-toast";

import {
  MIN_PASSPHRASE_LENGTH,
  type RestoreResult,
  backupFilename,
  saveBlob,
  useExportBackup,
  useRestoreBackup,
} from "../lib/backup";

function countsSummary(counts: Record<string, number>): string {
  return Object.entries(counts)
    .map(([name, n]) => `${n} ${name.replace(/_/g, " ")}`)
    .join(", ");
}

/** Backup & restore: download everything (encrypted with a passphrase by default) and restore it
 * on another machine. The LLM API key is never in a backup, so it has to be entered again there. */
export function BackupSection() {
  const { toast } = useToast();
  const exportBackup = useExportBackup();
  const restoreBackup = useRestoreBackup();

  const [encrypt, setEncrypt] = React.useState(true);
  const [passphrase, setPassphrase] = React.useState("");
  const [confirmPassphrase, setConfirmPassphrase] = React.useState("");

  const [file, setFile] = React.useState<File | null>(null);
  const [restorePassphrase, setRestorePassphrase] = React.useState("");
  const [restored, setRestored] = React.useState<RestoreResult | null>(null);
  const [restoreError, setRestoreError] = React.useState<string | null>(null);
  const fileInput = React.useRef<HTMLInputElement>(null);

  const passphraseTooShort = encrypt && passphrase.length > 0 && passphrase.length < MIN_PASSPHRASE_LENGTH;
  const passphraseMismatch = encrypt && confirmPassphrase.length > 0 && passphrase !== confirmPassphrase;
  const canDownload =
    !exportBackup.isPending && (!encrypt || (passphrase.length >= MIN_PASSPHRASE_LENGTH && passphrase === confirmPassphrase));

  const handleDownload = () => {
    exportBackup.mutate(
      { passphrase: encrypt ? passphrase : null },
      {
        onSuccess: (blob) => {
          saveBlob(blob, backupFilename());
          setPassphrase("");
          setConfirmPassphrase("");
          toast({ title: "Backup downloaded", description: "Keep the passphrase: it cannot be recovered." });
        },
        onError: (err) => toast({ title: "Could not create the backup", description: String(err), variant: "destructive" }),
      },
    );
  };

  const handleRestore = () => {
    if (!file) return;
    const confirmed = window.confirm(
      "Restoring replaces ALL current data (statements, transactions, tax forms, settings) with the contents of this backup. " +
        "Your current data is moved aside to a folder next to it, not deleted. Continue?",
    );
    if (!confirmed) return;
    setRestoreError(null);
    setRestored(null);
    restoreBackup.mutate(
      { file, passphrase: restorePassphrase },
      {
        onSuccess: (result) => {
          setRestored(result);
          setFile(null);
          setRestorePassphrase("");
          if (fileInput.current) fileInput.current.value = "";
          toast({ title: "Backup restored" });
        },
        onError: (err) => {
          setRestoreError(String(err));
          toast({ title: "Could not restore the backup", description: String(err), variant: "destructive" });
        },
      },
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Backup &amp; restore</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        <p className="text-xs text-muted-foreground">
          Move everything to another machine: download a backup here, then restore it there. A backup holds your
          database, kept statement PDFs, layout specs and settings. It never includes your LLM API key (that stays in
          this computer&apos;s credential store), so enter the key again on the new machine.
        </p>

        <div className="flex flex-col gap-3">
          <span className="text-sm font-medium">Download backup</span>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={encrypt} onChange={(e) => setEncrypt(e.target.checked)} />
            Encrypt with a passphrase (recommended)
          </label>
          {encrypt ? (
            <div className="flex flex-wrap items-start gap-2">
              <Input
                type="password"
                autoComplete="new-password"
                value={passphrase}
                onChange={(e) => setPassphrase(e.target.value)}
                placeholder={`Passphrase (${MIN_PASSPHRASE_LENGTH}+ characters)`}
                className="w-64"
                aria-label="Backup passphrase"
              />
              <Input
                type="password"
                autoComplete="new-password"
                value={confirmPassphrase}
                onChange={(e) => setConfirmPassphrase(e.target.value)}
                placeholder="Repeat passphrase"
                className="w-64"
                aria-label="Repeat backup passphrase"
              />
            </div>
          ) : (
            <p role="alert" className="text-xs text-destructive">
              Without a passphrase, anyone who gets the file can read your statements and tax forms.
            </p>
          )}
          {passphraseTooShort ? (
            <p className="text-xs text-destructive">Use at least {MIN_PASSPHRASE_LENGTH} characters.</p>
          ) : null}
          {passphraseMismatch ? <p className="text-xs text-destructive">The passphrases do not match.</p> : null}
          {encrypt ? (
            <p className="text-xs text-muted-foreground">
              There is no way to recover a lost passphrase, and nobody can open the backup without it.
            </p>
          ) : null}
          <div>
            <Button onClick={handleDownload} disabled={!canDownload}>
              {exportBackup.isPending ? "Preparing backup…" : "Download backup"}
            </Button>
          </div>
        </div>

        <div className="flex flex-col gap-3 border-t pt-4">
          <span className="text-sm font-medium">Restore from backup</span>
          <p className="text-xs text-muted-foreground">
            Replaces all current data with the backup. Your current data is kept in a folder beside it, not deleted.
          </p>
          <div className="flex flex-wrap items-start gap-2">
            <Input
              ref={fileInput}
              type="file"
              accept=".pfabackup"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="w-64"
              aria-label="Backup file"
            />
            <Input
              type="password"
              autoComplete="off"
              value={restorePassphrase}
              onChange={(e) => setRestorePassphrase(e.target.value)}
              placeholder="Passphrase (if encrypted)"
              className="w-64"
              aria-label="Restore passphrase"
            />
          </div>
          <div>
            <Button variant="destructive" onClick={handleRestore} disabled={!file || restoreBackup.isPending}>
              {restoreBackup.isPending ? "Restoring…" : "Restore from backup"}
            </Button>
          </div>
          {restoreError ? (
            <p role="alert" className="text-sm text-destructive">
              {restoreError}
            </p>
          ) : null}
          {restored ? (
            <div role="status" className="flex flex-col gap-1 text-sm">
              <span>
                Restored the backup made {restored.created_at}: {countsSummary(restored.counts)}.
              </span>
              {restored.migrated ? <span>Its database was upgraded to this version.</span> : null}
              {restored.previous_dir ? (
                <span className="text-xs text-muted-foreground">Your previous data was kept at {restored.previous_dir}</span>
              ) : null}
              <span className="text-xs text-muted-foreground">
                If you use an LLM that needs an API key, enter it again under LLM provider.
              </span>
            </div>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}
