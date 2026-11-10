import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type MockInstance, beforeEach, describe, expect, it, vi } from "vitest";

import { ToastContextProvider } from "@/components/ui/toast-provider";

import type { RestoreResult } from "../lib/backup";
import { BackupSection } from "./backup-section";

type ExportOptions = { onSuccess?: (blob: Blob) => void; onError?: (err: Error) => void };
type RestoreOptions = { onSuccess?: (result: RestoreResult) => void; onError?: (err: Error) => void };

const exportMutate = vi.fn<(vars: { passphrase: string | null }, opts: ExportOptions) => void>();
const restoreMutate = vi.fn<(vars: { file: File; passphrase: string }, opts: RestoreOptions) => void>();
const saveBlob = vi.fn<(blob: Blob, filename: string) => void>();

vi.mock("../lib/backup", () => ({
  MIN_PASSPHRASE_LENGTH: 8,
  backupFilename: () => "pfa-2026-01-01.pfabackup",
  saveBlob: (blob: Blob, filename: string) => {
    saveBlob(blob, filename);
  },
  useExportBackup: () => ({ mutate: exportMutate, isPending: false }),
  useRestoreBackup: () => ({ mutate: restoreMutate, isPending: false }),
}));

function renderSection() {
  return render(
    <ToastContextProvider>
      <BackupSection />
    </ToastContextProvider>,
  );
}

const PASSPHRASE = "correct horse battery";
let confirm: MockInstance<(message?: string) => boolean>;

describe("BackupSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  it("says the API key is never included", () => {
    renderSection();
    expect(screen.getByText(/never includes your LLM API key/i)).toBeInTheDocument();
  });

  it("encrypts by default and keeps Download disabled until a matching, long-enough passphrase", async () => {
    const user = userEvent.setup();
    renderSection();
    expect(screen.getByRole("checkbox", { name: /encrypt with a passphrase/i })).toBeChecked();
    const download = screen.getByRole("button", { name: "Download backup" });
    expect(download).toBeDisabled();

    await user.type(screen.getByLabelText("Backup passphrase"), "short");
    expect(screen.getByText(/at least 8 characters/i)).toBeInTheDocument();
    expect(download).toBeDisabled();

    await user.clear(screen.getByLabelText("Backup passphrase"));
    await user.type(screen.getByLabelText("Backup passphrase"), PASSPHRASE);
    await user.type(screen.getByLabelText("Repeat backup passphrase"), "something else");
    expect(screen.getByText(/passphrases do not match/i)).toBeInTheDocument();
    expect(download).toBeDisabled();

    await user.clear(screen.getByLabelText("Repeat backup passphrase"));
    await user.type(screen.getByLabelText("Repeat backup passphrase"), PASSPHRASE);
    expect(download).toBeEnabled();
  });

  it("downloads an encrypted backup with the passphrase and clears it afterwards", async () => {
    const user = userEvent.setup();
    renderSection();
    await user.type(screen.getByLabelText("Backup passphrase"), PASSPHRASE);
    await user.type(screen.getByLabelText("Repeat backup passphrase"), PASSPHRASE);
    await user.click(screen.getByRole("button", { name: "Download backup" }));

    expect(exportMutate).toHaveBeenCalledTimes(1);
    const [vars, options] = exportMutate.mock.calls[0] ?? [];
    expect(vars).toEqual({ passphrase: PASSPHRASE });

    const blob = new Blob(["archive"]);
    options?.onSuccess?.(blob);
    expect(saveBlob).toHaveBeenCalledWith(blob, "pfa-2026-01-01.pfabackup");
    expect(await screen.findByLabelText("Backup passphrase")).toHaveValue("");
  });

  it("opting out of encryption warns and sends no passphrase", async () => {
    const user = userEvent.setup();
    renderSection();
    await user.click(screen.getByRole("checkbox", { name: /encrypt with a passphrase/i }));
    expect(screen.getByRole("alert")).toHaveTextContent(/anyone who gets the file can read/i);
    expect(screen.queryByLabelText("Backup passphrase")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Download backup" }));
    expect(exportMutate.mock.calls[0]?.[0]).toEqual({ passphrase: null });
  });

  it("keeps Restore disabled until a file is chosen", () => {
    renderSection();
    expect(screen.getByRole("button", { name: "Restore from backup" })).toBeDisabled();
  });

  it("asks for confirmation before restoring and sends the file and passphrase", async () => {
    const user = userEvent.setup();
    renderSection();
    const file = new File(["bytes"], "mine.pfabackup", { type: "application/octet-stream" });
    await user.upload(screen.getByLabelText("Backup file"), file);
    await user.type(screen.getByLabelText("Restore passphrase"), PASSPHRASE);
    await user.click(screen.getByRole("button", { name: "Restore from backup" }));

    expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/replaces ALL current data/));
    expect(restoreMutate).toHaveBeenCalledTimes(1);
    const [vars] = restoreMutate.mock.calls[0] ?? [];
    expect(vars?.file).toBe(file);
    expect(vars?.passphrase).toBe(PASSPHRASE);
  });

  it("does nothing when the confirmation is declined", async () => {
    confirm.mockReturnValue(false);
    const user = userEvent.setup();
    renderSection();
    await user.upload(screen.getByLabelText("Backup file"), new File(["bytes"], "mine.pfabackup"));
    await user.click(screen.getByRole("button", { name: "Restore from backup" }));
    expect(restoreMutate).not.toHaveBeenCalled();
  });

  it("shows what was restored and where the previous data went", async () => {
    const user = userEvent.setup();
    renderSection();
    await user.upload(screen.getByLabelText("Backup file"), new File(["bytes"], "mine.pfabackup"));
    await user.click(screen.getByRole("button", { name: "Restore from backup" }));

    const [, options] = restoreMutate.mock.calls[0] ?? [];
    options?.onSuccess?.({
      created_at: "2026-10-30T10:00:00+00:00",
      counts: { users: 2, transactions: 1893 },
      migrated: true,
      previous_dir: "/data/PersonalFinanceAnalyzer.20261030T101500.before-restore",
    });

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("2 users, 1893 transactions");
    expect(status).toHaveTextContent("upgraded");
    expect(status).toHaveTextContent("before-restore");
    expect(status).toHaveTextContent(/enter it again/i);
  });

  it("shows the server's error message when restore fails", async () => {
    const user = userEvent.setup();
    renderSection();
    await user.upload(screen.getByLabelText("Backup file"), new File(["bytes"], "mine.pfabackup"));
    await user.click(screen.getByRole("button", { name: "Restore from backup" }));

    const [, options] = restoreMutate.mock.calls[0] ?? [];
    options?.onError?.(new Error("wrong passphrase, or the backup file is damaged or was modified"));

    expect(await screen.findByRole("alert")).toHaveTextContent(/wrong passphrase/i);
  });
});
