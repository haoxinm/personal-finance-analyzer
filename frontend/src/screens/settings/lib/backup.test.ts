import { afterEach, describe, expect, it, vi } from "vitest";

import { backupFilename, exportBackup, restoreBackup } from "./backup";

describe("backup API calls", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.head.querySelectorAll('meta[name="spend-token"]').forEach((el) => el.remove());
  });

  it("names the download like the CLI does", () => {
    expect(backupFilename(new Date("2026-10-30T12:00:00Z"))).toBe("spend-analyzer-2026-10-30.spendbackup");
  });

  it("POSTs the passphrase as JSON with the spend token and returns the file", async () => {
    const meta = document.createElement("meta");
    meta.name = "spend-token";
    meta.content = "test-token";
    document.head.appendChild(meta);
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(new Response("archive-bytes")));
    vi.stubGlobal("fetch", fetchMock);

    const blob = await exportBackup("correct horse battery");

    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url as string).toContain("/api/backup/export");
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("X-Spend-Token")).toBe("test-token");
    expect(JSON.parse(init?.body as string)).toEqual({ passphrase: "correct horse battery", encrypt: true });
    expect(await blob.text()).toBe("archive-bytes");
  });

  it("sends an explicit encrypt=false only when asked to skip the passphrase", async () => {
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(new Response("x")));
    vi.stubGlobal("fetch", fetchMock);
    await exportBackup(null);
    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(JSON.parse(init?.body as string)).toEqual({ encrypt: false });
  });

  it("uploads the file as multipart without setting a Content-Type, and returns the result", async () => {
    const result = { created_at: "2026-10-30T10:00:00+00:00", counts: { users: 1 }, migrated: false, previous_dir: null };
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response(JSON.stringify(result), { headers: { "Content-Type": "application/json" } })),
    );
    vi.stubGlobal("fetch", fetchMock);

    const file = new File(["bytes"], "mine.spendbackup");
    await expect(restoreBackup(file, "pw-pw-pw-pw")).resolves.toEqual(result);

    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url as string).toContain("/api/backup/restore");
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).has("Content-Type")).toBe(false);
    const form = init?.body as FormData;
    expect(form.get("passphrase")).toBe("pw-pw-pw-pw");
    expect((form.get("file") as File).name).toBe("mine.spendbackup");
  });

  it("omits an empty passphrase (unencrypted backups)", async () => {
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(new Response("{}")));
    vi.stubGlobal("fetch", fetchMock);
    await restoreBackup(new File(["bytes"], "mine.spendbackup"), "");
    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect((init?.body as FormData).has("passphrase")).toBe(false);
  });

  it("throws the server's detail message on failure", async () => {
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response(JSON.stringify({ detail: "wrong passphrase" }), { status: 400 })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await expect(restoreBackup(new File(["bytes"], "x.spendbackup"), "nope")).rejects.toThrow("wrong passphrase");
    await expect(exportBackup("x")).rejects.toThrow("wrong passphrase");
  });
});
