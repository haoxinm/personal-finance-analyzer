/**
 * Moving all data to another machine: download a backup, restore one.
 *
 *   POST /api/backup/export    body {passphrase, encrypt} -> the .spendbackup file
 *   POST /api/backup/restore   multipart {file, passphrase} -> {created_at, counts, migrated, previous_dir}
 *
 * The endpoints are in `src/api/schema.d.ts`, but the calls stay on `fetch` with hand-written
 * response types: the export is a binary download and the restore a multipart upload, neither of
 * which the typed client's JSON helpers cover (it does the same as `api-key.ts`). The archive
 * never contains the LLM API key (it lives in the OS credential store).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";

/** Keep in step with `crypto.MIN_PASSPHRASE_LENGTH` in the backend. */
export const MIN_PASSPHRASE_LENGTH = 8;

export interface RestoreResult {
  created_at: string;
  counts: Record<string, number>;
  migrated: boolean;
  previous_dir: string | null;
}

function readSpendToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const meta = document.querySelector('meta[name="spend-token"]');
  return meta?.getAttribute("content") ?? undefined;
}

function tokenHeaders(): Record<string, string> {
  const token = readSpendToken();
  return token ? { "X-Spend-Token": token } : {};
}

function backupUrl(path: string): string {
  const origin = typeof window === "undefined" ? "http://localhost" : window.location.origin;
  return new URL(`/api/backup/${path}`, origin).toString();
}

async function readErrorDetail(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      return String(body.detail);
    }
  } catch {
    // fall through to the status-based message below
  }
  return `${response.status} ${response.statusText}`;
}

/** `spend-analyzer-YYYY-MM-DD.spendbackup`, matching the CLI's default name. */
export function backupFilename(date: Date = new Date()): string {
  return `spend-analyzer-${date.toISOString().slice(0, 10)}.spendbackup`;
}

/** Asks the backend to build a backup. `passphrase: null` is an explicit "do not encrypt". */
export async function exportBackup(passphrase: string | null): Promise<Blob> {
  const response = await fetch(backupUrl("export"), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...tokenHeaders() },
    body: JSON.stringify(passphrase === null ? { encrypt: false } : { passphrase, encrypt: true }),
  });
  if (!response.ok) {
    throw new Error(await readErrorDetail(response));
  }
  return response.blob();
}

/** Uploads a backup; on success the server's data directory has been replaced by it. */
export async function restoreBackup(file: File, passphrase: string): Promise<RestoreResult> {
  const form = new FormData();
  form.append("file", file);
  if (passphrase) form.append("passphrase", passphrase);
  const response = await fetch(backupUrl("restore"), {
    method: "POST",
    headers: tokenHeaders(), // no Content-Type: the browser adds the multipart boundary
    body: form,
  });
  if (!response.ok) {
    throw new Error(await readErrorDetail(response));
  }
  return (await response.json()) as RestoreResult;
}

/** Hands `blob` to the browser as a download named `filename`. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export function useExportBackup() {
  return useMutation({
    mutationFn: ({ passphrase }: { passphrase: string | null }) => exportBackup(passphrase),
  });
}

export function useRestoreBackup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ file, passphrase }: { file: File; passphrase: string }) => restoreBackup(file, passphrase),
    // Everything on every screen came from the database that was just replaced.
    onSuccess: () => queryClient.invalidateQueries(),
  });
}
