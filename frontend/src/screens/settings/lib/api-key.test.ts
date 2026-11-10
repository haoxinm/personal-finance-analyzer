import { afterEach, describe, expect, it, vi } from "vitest";

import { clearApiKey, setApiKey } from "./api-key";

describe("setApiKey / clearApiKey", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.head.querySelectorAll('meta[name="pfa-token"]').forEach((el) => el.remove());
  });

  it("PUTs the key and base_url, never leaking the key into an error path", async () => {
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(new Response(null, { status: 204 })));
    vi.stubGlobal("fetch", fetchMock);

    await setApiKey("https://api.anthropic.com", "sk-secret");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url as string).toContain("/api/settings/api-key");
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(init?.body as string)).toEqual({ api_key: "sk-secret", base_url: "https://api.anthropic.com" });
  });

  it("sends the per-launch spend token when the meta tag is present", async () => {
    const meta = document.createElement("meta");
    meta.name = "pfa-token";
    meta.content = "test-token";
    document.head.appendChild(meta);
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(new Response(null, { status: 204 })));
    vi.stubGlobal("fetch", fetchMock);

    await setApiKey("https://api.openai.com", "sk-x");

    const [, init] = fetchMock.mock.calls[0] ?? [];
    const headers = new Headers(init?.headers);
    expect(headers.get("X-PFA-Token")).toBe("test-token");
  });

  it("throws with the server's detail message on failure", async () => {
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(
        new Response(JSON.stringify({ detail: "provider not configured" }), {
          status: 400,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(setApiKey("https://api.anthropic.com", "sk-secret")).rejects.toThrow("provider not configured");
  });

  it("DELETEs with base_url as a query parameter", async () => {
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(new Response(null, { status: 204 })));
    vi.stubGlobal("fetch", fetchMock);

    await clearApiKey("https://api.anthropic.com");

    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url as string).toContain("base_url=https%3A%2F%2Fapi.anthropic.com");
    expect(init?.method).toBe("DELETE");
  });

  it("falls back to the status line when the error body is not JSON", async () => {
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(new Response("nope", { status: 404 })));
    vi.stubGlobal("fetch", fetchMock);

    await expect(clearApiKey("https://api.anthropic.com")).rejects.toThrow("404");
  });
});
