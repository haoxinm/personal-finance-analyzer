import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Listener = (event: { matches: boolean }) => void;

/** A controllable `matchMedia("(prefers-color-scheme: dark)")`. */
function stubMatchMedia(initial: boolean) {
  const listeners = new Set<Listener>();
  const state = { matches: initial };
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      get matches() {
        return state.matches;
      },
      addEventListener: (_: string, l: Listener) => listeners.add(l),
      removeEventListener: (_: string, l: Listener) => listeners.delete(l),
    })),
  );
  return {
    set(matches: boolean) {
      state.matches = matches;
      listeners.forEach((l) => l({ matches }));
    },
  };
}

const root = () => document.documentElement;

describe("theme", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    root().classList.remove("dark");
    root().style.colorScheme = "";
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("follows a light OS by default", async () => {
    stubMatchMedia(false);
    const { initTheme } = await import("./theme");
    initTheme();
    expect(root().classList.contains("dark")).toBe(false);
    expect(root().style.colorScheme).toBe("light");
    expect(root().getAttribute("data-theme-preference")).toBe("system");
  });

  it("follows a dark OS by default", async () => {
    stubMatchMedia(true);
    const { initTheme } = await import("./theme");
    initTheme();
    expect(root().classList.contains("dark")).toBe(true);
    expect(root().style.colorScheme).toBe("dark");
  });

  it("lets an explicit choice override the OS, both ways", async () => {
    stubMatchMedia(false);
    window.localStorage.setItem("pfa:theme", "dark");
    const { initTheme, setThemePreference } = await import("./theme");
    initTheme();
    expect(root().classList.contains("dark")).toBe(true);
    setThemePreference("light");
    expect(root().classList.contains("dark")).toBe(false);

    vi.resetModules();
    stubMatchMedia(true);
    window.localStorage.setItem("pfa:theme", "light");
    const again = await import("./theme");
    again.initTheme();
    expect(root().classList.contains("dark")).toBe(false);
  });

  it('stores "system" by removing the key', async () => {
    stubMatchMedia(false);
    const { setThemePreference } = await import("./theme");
    setThemePreference("dark");
    expect(window.localStorage.getItem("pfa:theme")).toBe("dark");
    setThemePreference("system");
    expect(window.localStorage.getItem("pfa:theme")).toBeNull();
  });

  it("treats an invalid stored value as system", async () => {
    stubMatchMedia(false);
    window.localStorage.setItem("pfa:theme", "purple");
    const { readThemePreference } = await import("./theme");
    expect(readThemePreference()).toBe("system");
  });

  it("still renders light when localStorage throws", async () => {
    stubMatchMedia(false);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const { initTheme } = await import("./theme");
    expect(() => initTheme()).not.toThrow();
    expect(root().classList.contains("dark")).toBe(false);
  });

  it("works without matchMedia (jsdom)", async () => {
    vi.stubGlobal("matchMedia", undefined);
    const { initTheme, systemPrefersDark } = await import("./theme");
    expect(systemPrefersDark()).toBe(false);
    expect(() => initTheme()).not.toThrow();
  });

  it("flips with the OS while on system", async () => {
    const media = stubMatchMedia(false);
    const { initTheme } = await import("./theme");
    initTheme();
    expect(root().classList.contains("dark")).toBe(false);
    media.set(true);
    expect(root().classList.contains("dark")).toBe(true);
    media.set(false);
    expect(root().classList.contains("dark")).toBe(false);
  });

  it("does not flip with the OS when a theme is chosen", async () => {
    const media = stubMatchMedia(false);
    window.localStorage.setItem("pfa:theme", "light");
    const { initTheme } = await import("./theme");
    initTheme();
    media.set(true);
    expect(root().classList.contains("dark")).toBe(false);
  });
});
