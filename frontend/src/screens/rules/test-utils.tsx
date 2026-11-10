import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import { ToastContextProvider } from "@/components/ui/toast-provider";

/** A fresh, no-retry `QueryClient` per test so failures surface immediately. */
export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

export function TestProviders({
  children,
  queryClient = createTestQueryClient(),
  initialEntries = ["/spending/rules"],
}: {
  children: React.ReactNode;
  queryClient?: QueryClient;
  initialEntries?: string[];
}) {
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        initialEntries={initialEntries}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ToastContextProvider>{children}</ToastContextProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

export type ApiHandler = (req: Request) => unknown;

/**
 * `@/api/client` reads `globalThis.fetch` once, as a default parameter, when `createClient()`
 * runs at that module's top level (`openapi-fetch`'s `fetch: baseFetch = globalThis.fetch`).
 * Stubbing `fetch` from inside a test body is too late — the module has already captured the
 * real one by then. Every test file that renders a component reaching the API therefore mocks
 * the module itself:
 *
 * ```ts
 * vi.mock("@/api/client", async () => {
 *   vi.stubGlobal("fetch", dynamicMockFetch);
 *   return vi.importActual("@/api/client");
 * });
 * ```
 *
 * — which runs once, lazily, the first time something imports `@/api/client`, stubs `fetch`
 * *before* evaluating the real module, and returns that real (now correctly-wired) module. Each
 * `it()` then only calls `setMockHandlers(...)`; there is no need to reset the module registry
 * (which would otherwise duplicate the toast context module and break `useToast`).
 */
let handlers: Record<string, ApiHandler> = {};

export function setMockHandlers(next: Record<string, ApiHandler>): void {
  handlers = next;
}

/** Keyed by `"<METHOD> <path>"`; `path` is matched against the request's pathname by substring. */
export async function dynamicMockFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const request = input instanceof Request ? input : new Request(input, init);
  const method = (init?.method ?? request.method ?? "GET").toUpperCase();
  const url = new URL(request.url);
  const key = Object.keys(handlers).find((k) => {
    const [m, path] = k.split(" ", 2);
    return m === method && path !== undefined && url.pathname.includes(path);
  });
  if (!key) {
    throw new Error(`No mock handler for ${method} ${url.pathname}`);
  }
  const result = await handlers[key]?.(request);
  if (result === undefined) {
    return new Response(null, { status: 204 });
  }
  return new Response(JSON.stringify(result), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

/** Convenience re-export so test files can `vi.mock(...)` without importing `vitest` twice. */
export { vi };
