import { afterEach, describe, expect, it, vi } from "vitest";

import { backupFilename, encodePassphrase, exportBackup, parseSkipped, restoreBackup } from "./backup";

describe("backup API calls", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.head.querySelectorAll('meta[name="pfa-token"]').forEach((el) => el.remove());
  });

  it("names the download like the CLI does", () => {
    expect(backupFilename(new Date("2026-10-30T12:00:00Z"))).toBe("pfa-2026-10-30.pfabackup");
  });

  it("POSTs the passphrase as JSON with the spend token and returns the file", async () => {
    const meta = document.createElement("meta");
    meta.name = "pfa-token";
    meta.content = "test-token";
    document.head.appendChild(meta);
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(new Response("archive-bytes")));
    vi.stubGlobal("fetch", fetchMock);

    const { blob, skipped } = await exportBackup("correct horse battery");
    expect(skipped).toBeNull();

    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url as string).toContain("/api/backup/export");
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("X-PFA-Token")).toBe("test-token");
    expect(JSON.parse(init?.body as string)).toEqual({ passphrase: "correct horse battery", encrypt: true });
    expect(await blob.text()).toBe("archive-bytes");
  });

  it("reports the files the server left out of the backup", async () => {
    const header = encodeURIComponent(JSON.stringify({ count: 2, examples: ["Scan é.pdf", "big.pdf"] }));
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response("x", { headers: { "X-Backup-Skipped": header } })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { skipped } = await exportBackup(null);
    expect(skipped).toEqual({ count: 2, examples: ["Scan é.pdf", "big.pdf"] });
    expect(parseSkipped("not%json")).toBeNull();
    expect(parseSkipped(null)).toBeNull();
  });

  it("sends an explicit encrypt=false only when asked to skip the passphrase", async () => {
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(new Response("x")));
    vi.stubGlobal("fetch", fetchMock);
    await exportBackup(null);
    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(JSON.parse(init?.body as string)).toEqual({ encrypt: false });
  });

  it("uploads the file as the raw body with the passphrase in a header, and returns the result", async () => {
    const result = { created_at: "2026-10-30T10:00:00+00:00", counts: { users: 1 }, migrated: false, previous_dir: null };
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response(JSON.stringify(result), { headers: { "Content-Type": "application/json" } })),
    );
    vi.stubGlobal("fetch", fetchMock);

    const file = new File(["bytes"], "mine.pfabackup");
    await expect(restoreBackup(file, "pw-pw-pw-pw")).resolves.toEqual(result);

    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url as string).toContain("/api/backup/restore");
    expect(init?.method).toBe("POST");
    const headers = new Headers(init?.headers);
    expect(headers.get("Content-Type")).toBe("application/octet-stream");
    expect(headers.get("X-Backup-Passphrase")).toBe(btoa("pw-pw-pw-pw"));
    expect(init?.body).toBe(file); // the File itself: streamed from disk, never FormData
  });

  it("base64-encodes the UTF-8 bytes of a non-ASCII passphrase", () => {
    const encoded = encodePassphrase("pässwörd-日本語");
    const decoded = new TextDecoder().decode(Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0)));
    expect(decoded).toBe("pässwörd-日本語");
  });

  it("omits an empty passphrase (unencrypted backups)", async () => {
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(new Response("{}")));
    vi.stubGlobal("fetch", fetchMock);
    await restoreBackup(new File(["bytes"], "mine.pfabackup"), "");
    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(new Headers(init?.headers).has("X-Backup-Passphrase")).toBe(false);
  });

  it("explains a dropped connection (the server refuses an oversized upload without reading it)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() => Promise.reject(new TypeError("Failed to fetch"))),
    );
    await expect(restoreBackup(new File(["bytes"], "x.pfabackup"), "")).rejects.toThrow(/did not complete/);
  });

  it("throws the server's detail message on failure", async () => {
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response(JSON.stringify({ detail: "wrong passphrase" }), { status: 400 })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await expect(restoreBackup(new File(["bytes"], "x.pfabackup"), "nope")).rejects.toThrow("wrong passphrase");
    await expect(exportBackup("x")).rejects.toThrow("wrong passphrase");
  });
});
