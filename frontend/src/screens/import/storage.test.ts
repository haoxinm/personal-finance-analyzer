import { afterEach, describe, expect, it } from "vitest";

import {
  isSkipConfirmForIssuer,
  setSkipConfirmForIssuer,
} from "./storage";

describe("import screen storage", () => {
  afterEach(() => {
    window.localStorage.clear();
  });

  it("has no issuer skipping the confirmation step by default", () => {
    expect(isSkipConfirmForIssuer(7)).toBe(false);
  });

  it("toggles the per-issuer skip-confirmation flag independently per issuer", () => {
    setSkipConfirmForIssuer(7, true);
    expect(isSkipConfirmForIssuer(7)).toBe(true);
    expect(isSkipConfirmForIssuer(9)).toBe(false);

    setSkipConfirmForIssuer(9, true);
    expect(isSkipConfirmForIssuer(7)).toBe(true);
    expect(isSkipConfirmForIssuer(9)).toBe(true);

    setSkipConfirmForIssuer(7, false);
    expect(isSkipConfirmForIssuer(7)).toBe(false);
    expect(isSkipConfirmForIssuer(9)).toBe(true);
  });

  it("carries a skip-confirmation list saved before the rename over to the new key", () => {
    window.localStorage.setItem("spend-analyzer:import:skip-confirm-issuer-ids", "[7]");
    expect(isSkipConfirmForIssuer(7)).toBe(true);
    expect(window.localStorage.getItem("pfa:import:skip-confirm-issuer-ids")).toBe("[7]");
    expect(window.localStorage.getItem("spend-analyzer:import:skip-confirm-issuer-ids")).toBeNull();
  });

  it("survives a corrupt skip-confirmation value", () => {
    window.localStorage.setItem("pfa:import:skip-confirm-issuer-ids", "{not json");
    expect(isSkipConfirmForIssuer(1)).toBe(false);
  });
});
