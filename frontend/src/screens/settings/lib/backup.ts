/**
 * Moving all data to another machine: download a backup, restore one.
 *
 *   POST /api/backup/export    body {passphrase, encrypt} -> the .pfabackup file
 *   POST /api/backup/restore   the raw file as an application/octet-stream body, the passphrase in the
 *                              X-Backup-Passphrase header (base64 of its UTF-8 bytes)
 *                              -> {created_at, counts, migrated, previous_dir,
 *                              documents_restored, documents_unrelated, ...}
 *
 * The endpoints are in `src/api/schema.d.ts`, but the calls stay on `fetch` with hand-written
 * response types: the export is a binary download and the restore a raw-body upload, neither of
 * which the typed client's JSON helpers cover (it does the same as `api-key.ts`). The restore is
 * not multipart on purpose: a multipart upload is spooled by the server into its system temp
 * directory (a second plaintext copy outside the data directory) before it is looked at, and a
 * raw body can be streamed straight into a private work directory and cut off at the size cap.
 * The browser sends a `File` body straight from disk, without reading it into memory. The archive
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
  /** PDFs written into the documents folder, and ones already there with the same content. */
  documents_restored?: number;
  documents_already_present?: number;
  /** Other files in the documents folder that are not part of the backup (left untouched). */
  documents_unrelated?: number;
  /** Set when some PDFs could not be moved into place and are still in this hidden folder. */
  documents_pending?: string | null;
}

function readPfaToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const meta = document.querySelector('meta[name="pfa-token"]');
  return meta?.getAttribute("content") ?? undefined;
}

function tokenHeaders(): Record<string, string> {
  const token = readPfaToken();
  return token ? { "X-PFA-Token": token } : {};
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

/** `pfa-YYYY-MM-DD.pfabackup`, matching the CLI's default name. */
export function backupFilename(date: Date = new Date()): string {
  return `pfa-${date.toISOString().slice(0, 10)}.pfabackup`;
}

/** Files the backend left out of the download (a name that cannot be stored portably, or larger than
 * a restore accepts): the total and the first few names. */
export interface SkippedFiles {
  count: number;
  examples: string[];
}

export interface ExportedBackup {
  blob: Blob;
  /** Present when some files are NOT in the backup. */
  skipped: SkippedFiles | null;
}

/** Reads the `X-Backup-Skipped` header (URL-quoted JSON); `null` when absent or unreadable. */
export function parseSkipped(header: string | null): SkippedFiles | null {
  if (!header) return null;
  try {
    const parsed: unknown = JSON.parse(decodeURIComponent(header));
    if (parsed && typeof parsed === "object" && "count" in parsed && "examples" in parsed) {
      const { count, examples } = parsed;
      if (typeof count === "number" && Array.isArray(examples)) {
        return { count, examples: examples.map(String) };
      }
    }
  } catch {
    // an unreadable header just means no warning
  }
  return null;
}

/** Asks the backend to build a backup. `passphrase: null` is an explicit "do not encrypt". */
export async function exportBackup(passphrase: string | null): Promise<ExportedBackup> {
  const response = await fetch(backupUrl("export"), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...tokenHeaders() },
    body: JSON.stringify(passphrase === null ? { encrypt: false } : { passphrase, encrypt: true }),
  });
  if (!response.ok) {
    throw new Error(await readErrorDetail(response));
  }
  return { blob: await response.blob(), skipped: parseSkipped(response.headers.get("X-Backup-Skipped")) };
}

/** Header values are latin-1, so the passphrase travels as base64 of its UTF-8 bytes. */
export function encodePassphrase(passphrase: string): string {
  let binary = "";
  for (const byte of new TextEncoder().encode(passphrase)) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/** Uploads a backup; on success the server's data directory has been replaced by it. */
export async function restoreBackup(file: File, passphrase: string): Promise<RestoreResult> {
  const headers: Record<string, string> = { "Content-Type": "application/octet-stream", ...tokenHeaders() };
  if (passphrase) headers["X-Backup-Passphrase"] = encodePassphrase(passphrase);
  let response: Response;
  try {
    response = await fetch(backupUrl("restore"), { method: "POST", headers, body: file });
  } catch {
    // The server answers an oversized upload (413) without reading the rest of the body, which a
    // browser can report as a dropped connection instead of the response.
    throw new Error(
      "The upload did not complete. The server may have refused the file (a backup over 2 GB is refused), or the connection dropped.",
    );
  }
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
