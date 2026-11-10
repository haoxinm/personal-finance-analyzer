import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("apiClient", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("issues a GET against /api/<path> and returns typed data", async () => {
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(
        new Response(JSON.stringify([{ id: 1, name: "Alex", is_default: true }]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { apiClient } = await import("./client");
    const { data, error } = await apiClient.GET("/users");

    expect(error).toBeUndefined();
    expect(data).toEqual([{ id: 1, name: "Alex", is_default: true }]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const request = fetchMock.mock.calls.at(0)?.[0] as Request | undefined;
    expect(request?.url).toContain("/api/users");
  });

  it("sends the per-launch token from the pfa-token meta tag, when present", async () => {
    const meta = document.createElement("meta");
    meta.name = "pfa-token";
    meta.content = "test-token";
    document.head.appendChild(meta);

    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(new Response("[]", { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);

    const { apiClient } = await import("./client");
    await apiClient.GET("/users");

    const request = fetchMock.mock.calls.at(0)?.[0] as Request | undefined;
    expect(request?.headers.get("X-PFA-Token")).toBe("test-token");

    document.head.removeChild(meta);
  });
});
