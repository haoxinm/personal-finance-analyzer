import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient, throwIfError, type components } from "@/api/client";

import type { SpecFieldError } from "./spec-types";

/**
 * Screen-local query/mutation hooks for the layout mapper (Phase 3 common brief: "Put
 * screen-specific query hooks inside your screen dir"). Nothing here is shared with another
 * screen.
 */

/**
 * Maps a FastAPI 422 `HTTPValidationError` (§3.12, `detail: ValidationError[]`) to this screen's
 * own `SpecFieldError[]`, so a `spec_yaml` problem the server caught (not just this screen's own
 * client-side `validateSpec`) renders inline next to the field it complains about. Returns `null`
 * for anything else (a network error, a non-422 status), so the caller falls back to a toast.
 */
export function parseValidationErrors(error: unknown): SpecFieldError[] | null {
  if (typeof error !== "object" || error === null || !("detail" in error)) return null;
  const detail: unknown = error.detail;
  if (!Array.isArray(detail)) return null;
  const errors: SpecFieldError[] = [];
  for (const item of detail as unknown[]) {
    if (typeof item !== "object" || item === null) continue;
    const loc: unknown = "loc" in item ? item.loc : undefined;
    const msg: unknown = "msg" in item ? item.msg : undefined;
    const locParts: (string | number)[] = Array.isArray(loc)
      ? (loc as unknown[]).filter((p): p is string | number => typeof p === "string" || typeof p === "number")
      : [];
    // loc is typically ["body", "spec_yaml"] or ["body", "spec_yaml", "columns", 2, "x0"];
    // drop the leading "body" and join the rest as this screen's own field paths use.
    const field = locParts.filter((p) => p !== "body").join(".") || "spec_yaml";
    errors.push({ field, message: typeof msg === "string" ? msg : "Invalid value" });
  }
  return errors;
}

export function useStatement(statementId: number | null) {
  return useQuery({
    queryKey: ["layout-mapper", "statement", statementId],
    enabled: statementId != null,
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/statements/{statement_id}", {
        params: { path: { statement_id: statementId as number } },
      });
      throwIfError(error);
      return data;
    },
  });
}

/**
 * `GET /statements/{id}/preview` (§3.12): the statement's extracted words with their PDF-point
 * coordinates, for click-to-map (§2d.2 step 2) — clicking a word on the rendered page fills in a
 * column's `x0`/`x1` instead of reading them off by eye.
 */
export function useStatementPreview(statementId: number | null, page = 1) {
  return useQuery({
    queryKey: ["layout-mapper", "statement-preview", statementId, page],
    enabled: statementId != null,
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/statements/{statement_id}/preview", {
        params: { path: { statement_id: statementId as number }, query: { page } },
      });
      throwIfError(error);
      return data ?? null;
    },
  });
}

/**
 * `POST /layout-specs/dry-run` (§3.12): runs a spec against a statement's real text with **no
 * side effects** — the true "this yields N transactions totalling X" live feedback §2d.2 asks
 * for. Does not `throwIfError`: a 422 here is a spec problem to show inline
 * (`parseValidationErrors`), not an exceptional failure.
 */
export function useDryRunLayoutSpec() {
  return useMutation({
    mutationFn: async (vars: { statementId: number; specYaml: string }) => {
      const { data, error } = await apiClient.POST("/layout-specs/dry-run", {
        body: { statement_id: vars.statementId, spec_yaml: vars.specYaml },
      });
      if (error) {
        const fieldErrors = parseValidationErrors(error);
        if (fieldErrors) return { ok: false as const, fieldErrors };
        throwIfError(error);
      }
      if (!data) throw new Error("dry-run returned no data");
      return { ok: true as const, result: data };
    },
  });
}

/** Every statement, across every user — feeds the picker this screen shows when it is opened
 * with no `?statement_id=` (§2d.2's entry point is normally a deep link, but a person can land
 * here directly too, and should get a list to choose from rather than an instruction to hand-edit
 * the URL). */
export function useAllStatements() {
  return useQuery({
    queryKey: ["layout-mapper", "all-statements"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/statements", { params: { query: {} } });
      throwIfError(error);
      return data ?? [];
    },
  });
}

/** All statements for `userId`, used to find the account's previous statement for the drift diff
 * (§2d.1) — there is no per-account endpoint, so this is filtered client-side (`drift.ts`). */
export function useStatementsForDrift(userId: number | null) {
  return useQuery({
    queryKey: ["layout-mapper", "statements", userId],
    enabled: userId != null,
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/statements", {
        params: { query: { user_id: userId ?? undefined } },
      });
      throwIfError(error);
      return data ?? [];
    },
  });
}

export function useIssuers() {
  return useQuery({
    queryKey: ["layout-mapper", "issuers"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/issuers");
      throwIfError(error);
      return data ?? [];
    },
  });
}

export function useLayoutSpecs() {
  return useQuery({
    queryKey: ["layout-mapper", "layout-specs"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/layout-specs");
      throwIfError(error);
      return data ?? [];
    },
  });
}

function invalidateSpecs(queryClient: ReturnType<typeof useQueryClient>) {
  void queryClient.invalidateQueries({ queryKey: ["layout-mapper", "layout-specs"] });
}

/** Thrown by `useCreateLayoutSpec`/`useReviseLayoutSpec` when the server rejects a `spec_yaml`
 * with a 422 (§3.12) whose `detail` names the offending field(s) — carries those alongside a
 * human-readable message so a caller can show them next to the field instead of only a toast. */
export class SpecValidationError extends Error {
  constructor(public readonly fieldErrors: SpecFieldError[]) {
    super(fieldErrors.map((e) => `${e.field}: ${e.message}`).join("; "));
    this.name = "SpecValidationError";
  }
}

function throwSpecError(error: unknown): void {
  const fieldErrors = parseValidationErrors(error);
  if (fieldErrors && fieldErrors.length > 0) throw new SpecValidationError(fieldErrors);
  throwIfError(error);
}

export function useCreateLayoutSpec() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["LayoutSpecCreate"]) => {
      const { data, error } = await apiClient.POST("/layout-specs", { body });
      throwSpecError(error);
      return data;
    },
    onSuccess: () => invalidateSpecs(queryClient),
  });
}

export function useReviseLayoutSpec() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      specId,
      body,
    }: {
      specId: number;
      body: components["schemas"]["LayoutSpecCreate"];
    }) => {
      const { data, error } = await apiClient.POST("/layout-specs/{spec_id}/revise", {
        params: { path: { spec_id: specId } },
        body,
      });
      throwSpecError(error);
      return data;
    },
    onSuccess: () => invalidateSpecs(queryClient),
  });
}

export function useApproveLayoutSpec() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (specId: number) => {
      const { data, error } = await apiClient.POST("/layout-specs/{spec_id}/approve", {
        params: { path: { spec_id: specId } },
      });
      throwIfError(error);
      return data;
    },
    onSuccess: () => invalidateSpecs(queryClient),
  });
}

/** `GET /layout-specs/{id}/export` returns `text/plain` (YAML), not JSON — `openapi-fetch`
 * still parses the response body as text here since the schema has no typed content type. */
export async function exportLayoutSpec(specId: number): Promise<string> {
  const { data, error } = await apiClient.GET("/layout-specs/{spec_id}/export", {
    params: { path: { spec_id: specId } },
    parseAs: "text",
  });
  throwIfError(error);
  return data ?? "";
}

/**
 * Runs a spec against the real statement to get an honest transaction count / reconciliation
 * delta (§2d.2's "live … feedback", degraded — see this WP's report on the missing dry-run
 * endpoint): `POST /statements/{id}/extract` when the statement is still `awaiting_extractor`
 * (nothing parsed yet), otherwise `POST /statements/{id}/reparse`, which "rebuilds that
 * statement's rows" (§3.12) using the given spec. Both **persist**, unlike a true preview.
 */
export function useTestLayoutSpec() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      statementId,
      layoutSpecId,
      issuerId,
      alreadyParsed,
    }: {
      statementId: number;
      layoutSpecId: number;
      issuerId: number;
      alreadyParsed: boolean;
    }) => {
      if (alreadyParsed) {
        const { data, error } = await apiClient.POST("/statements/{statement_id}/reparse", {
          params: { path: { statement_id: statementId } },
          body: { issuer_id: issuerId, layout_spec_id: layoutSpecId, parser_id: null, remember: false, discard_edits: false },
        });
        throwIfError(error);
        return data;
      }
      const { data, error } = await apiClient.POST("/statements/{statement_id}/extract", {
        params: { path: { statement_id: statementId } },
        body: { issuer_id: issuerId, layout_spec_id: layoutSpecId, remember: false },
      });
      throwIfError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["layout-mapper", "statement"] });
      void queryClient.invalidateQueries({ queryKey: ["layout-mapper", "statements"] });
    },
  });
}
