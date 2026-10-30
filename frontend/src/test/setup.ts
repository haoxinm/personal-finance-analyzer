import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

import "@testing-library/jest-dom/vitest";

// `globals: false` in vitest.config.ts means React Testing Library's own auto-cleanup
// (which looks for a global `afterEach`) never registers, so it is wired up explicitly here.
afterEach(() => {
  cleanup();
});

// jsdom has no `ResizeObserver`, but `@tanstack/react-virtual` (the transactions grid's row
// windowing) constructs one unconditionally to observe the scroll container. A no-op polyfill
// is enough here: tests drive virtualization by setting `offsetHeight`/`offsetWidth` directly
// (see below) and firing a `scroll` event, not by resizing the element.
if (typeof globalThis.ResizeObserver === "undefined") {
  class NoopResizeObserver implements ResizeObserver {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  globalThis.ResizeObserver = NoopResizeObserver;
}

// jsdom defines `offsetWidth`/`offsetHeight` as getters that always return 0 (it does no real
// layout). `@tanstack/react-virtual`'s default rect observer reads exactly those two properties
// to size its viewport (`getRect` in `virtual-core`), so left at jsdom's default it windows
// every virtualized grid down to zero visible rows in every test, not just the transactions
// grid's own. Replacing the getter with a generous fixed default — still overridable per element
// with its own `Object.defineProperty` where a test cares about the exact size — keeps
// virtualized grids rendering a normal-looking window under jsdom.
Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, value: 600 });
Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, value: 800 });

// jsdom has no pointer-capture API, which Radix's toast calls from its pointer handlers; without
// these stubs a (user-event) click on a toast's action button throws. Real browsers have them.
if (typeof Element.prototype.hasPointerCapture !== "function") {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
}
