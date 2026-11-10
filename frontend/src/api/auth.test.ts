import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function addMeta(name: string, content: string): HTMLMetaElement {
  const meta = document.createElement("meta");
  meta.name = name;
  meta.content = content;
  document.head.appendChild(meta);
  return meta;
}

describe("sign-in state", () => {
  const originalLocation = window.location;
  let assign: ReturnType<typeof vi.fn>;
  let metas: HTMLMetaElement[];

  beforeEach(() => {
    vi.resetModules();
    metas = [];
    assign = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { origin: "http://localhost:3000", pathname: "/tax/documents", search: "?year=2025", assign },
    });
  });

  afterEach(() => {
    metas.forEach((m) => m.remove());
    Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("knows there is no login on a plain localhost install", async () => {
    const { authEnabled } = await import("./auth");
    expect(authEnabled()).toBe(false);
  });

  it("detects the login from the pfa-auth meta tag", async () => {
    metas.push(addMeta("pfa-auth", "1"));
    const { authEnabled } = await import("./auth");
    expect(authEnabled()).toBe(true);
  });

  it("sends the browser to the login page, which returns to the current page", async () => {
    const { redirectToLogin } = await import("./auth");
    redirectToLogin();
    expect(assign).toHaveBeenCalledWith("/login?next=%2Ftax%2Fdocuments%3Fyear%3D2025");
  });

  it("signs out with the per-launch token, then shows the login page", async () => {
    metas.push(addMeta("pfa-token", "tok"));
    const fetchMock = vi.fn<typeof fetch>(() => Promise.resolve(new Response("{}", { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    const { signOut } = await import("./auth");
    await signOut();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/logout");
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual({ "X-PFA-Token": "tok" });
    expect(assign).toHaveBeenCalledWith("/login");
  });

  it("still lands on the login page when the sign-out request fails", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>(() => Promise.reject(new TypeError("offline"))));
    const { signOut } = await import("./auth");
    await expect(signOut()).rejects.toThrow("offline");
    expect(assign).toHaveBeenCalledWith("/login");
  });

  it("api client: a 401 behind the login goes back to the sign-in page", async () => {
    metas.push(addMeta("pfa-auth", "1"));
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() => Promise.resolve(new Response('{"detail":"authentication required"}', { status: 401 }))),
    );
    const { apiClient } = await import("./client");
    await apiClient.GET("/users");
    expect(assign).toHaveBeenCalledWith("/login?next=%2Ftax%2Fdocuments%3Fyear%3D2025");
  });

  it("api client: a 401 without a login (localhost) is just an error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() => Promise.resolve(new Response('{"detail":"nope"}', { status: 401 }))),
    );
    const { apiClient } = await import("./client");
    const { error } = await apiClient.GET("/users");
    expect(error).toBeDefined();
    expect(assign).not.toHaveBeenCalled();
  });
});
