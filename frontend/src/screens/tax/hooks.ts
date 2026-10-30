import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { useSearchParams } from "react-router-dom";

import {
  ApiError,
  apiClient,
  throwIfError,
  type components,
} from "@/api/client";
import { useSettings } from "@/api/hooks";

import { resolveYear } from "./logic";

const ROOT_KEY = ["tax"] as const;
/** Root key of every LLM "what would be sent" preview (tax and statements). */
const PREVIEW_KEY = ["llm-preview"] as const;

/** URL search param holding the tax year being viewed, shared by the Overview and Documents tabs. */
export const TAX_YEAR_PARAM = "year";

/** Search params that deep-link a document: `?doc=<id>&add=1` opens its add-form editor. */
export const DOCUMENT_PARAM = "doc";
export const ADD_PARAM = "add";

function parseYearParam(raw: string | null): number | null {
  const year = raw === null ? NaN : Number(raw);
  return Number.isInteger(year) ? year : null;
}

function useYearParamSetter() {
  const [, setSearchParams] = useSearchParams();
  return React.useCallback(
    (year: number | null) =>
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (year === null) next.delete(TAX_YEAR_PARAM);
          else next.set(TAX_YEAR_PARAM, String(year));
          return next;
        },
        { replace: true },
      ),
    [setSearchParams],
  );
}

/**
 * The year shown on the Overview: `?year=` if it is one of `years`, else the newest. Lives in the
 * URL so a year is linkable and survives moving between the Income & Tax tabs. In compare mode both
 * panes read the same `?year=` (the year is a view setting, like the date filters, not per user).
 */
export function useTaxYear(
  years: readonly number[],
  /** Used instead of the newest year when `?year=` names none of `years`. */
  fallback?: number,
): [number | null, (year: number) => void] {
  const [searchParams] = useSearchParams();
  const setYear = useYearParamSetter();
  const selected = parseYearParam(searchParams.get(TAX_YEAR_PARAM));
  const year = selected !== null && years.includes(selected) ? selected : fallback ?? resolveYear(null, years);
  return [year, setYear];
}

/** The Documents tab's year filter: the raw `?year=` (`null` = every year, including undated). */
export function useTaxYearFilter(): [number | null, (year: number | null) => void] {
  const [searchParams] = useSearchParams();
  return [parseYearParam(searchParams.get(TAX_YEAR_PARAM)), useYearParamSetter()];
}

export type TaxImportResponse = components["schemas"]["TaxImportResponse"];

export function useTaxYears(userIds: number[] | undefined, enabled = true) {
  return useQuery({
    enabled,
    queryKey: [...ROOT_KEY, "years", { userIds }],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/tax/years", {
        params: { query: { user_ids: userIds } },
      });
      throwIfError(error);
      return Array.isArray(data) ? data : [];
    },
  });
}

/** All years' summaries for the selected users (combined when more than one). */
export function useTaxSummary(userIds: number[] | undefined, enabled = true) {
  return useQuery({
    enabled,
    queryKey: [...ROOT_KEY, "summary", { userIds }],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/tax/summary", {
        params: { query: { user_ids: userIds, currency: "USD" } },
      });
      throwIfError(error);
      return Array.isArray(data) ? data : [];
    },
  });
}

export function useTaxIncomeByPayer(
  userIds: number[] | undefined,
  enabled = true,
) {
  return useQuery({
    enabled,
    queryKey: [...ROOT_KEY, "payers", { userIds }],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/tax/income-by-payer", {
        params: { query: { user_ids: userIds, currency: "USD" } },
      });
      throwIfError(error);
      return Array.isArray(data) ? data : [];
    },
  });
}

export function useTaxDocuments(userId: number | null, enabled = true) {
  return useQuery({
    enabled,
    queryKey: [...ROOT_KEY, "documents", { userId }],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/tax/documents", {
        params: { query: { user_id: userId ?? undefined } },
      });
      throwIfError(error);
      return Array.isArray(data) ? data : [];
    },
  });
}

export function useUploadTaxDocument() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      file,
      userId,
      taxYear,
    }: {
      file: File;
      userId: number;
      taxYear?: number;
    }): Promise<TaxImportResponse> => {
      const form = new FormData();
      form.append("file", file);
      form.append("user_id", String(userId));
      if (taxYear !== undefined) form.append("tax_year", String(taxYear));
      const { data, error } = await apiClient.POST("/tax/documents", {
        // A real FormData is passed through by openapi-fetch; the generated type describes the
        // decoded fields (same cast as the Import screen).
        body: form as unknown as components["schemas"]["Body_upload_tax_document_api_tax_documents_post"],
      });
      throwIfError(error);
      if (!data) throw new Error("upload returned no data");
      return data;
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ROOT_KEY });
    },
  });
}

/**
 * Every id-addressed tax call sends the owning user's id (`ownerId`, the document's `user_id`), so
 * the server rejects (404) an id that belongs to someone else. Required: the UI always has it from the
 * documents list (and updates it from the response after a move).
 */
export type OwnerId = number;
const owner = (ownerId: OwnerId) => ({ user_id: ownerId });

export function useDeleteTaxDocument(ownerId: OwnerId) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => {
      const { error } = await apiClient.DELETE("/tax/documents/{document_id}", {
        params: { path: { document_id: id }, query: owner(ownerId) },
      });
      throwIfError(error);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ROOT_KEY });
    },
  });
}

// ---- Manual overrides ------------------------------------------------------------------------

type Schemas = components["schemas"];
export type TaxFormCreate = Schemas["TaxFormCreate"];
export type TaxFormPatch = Schemas["TaxFormPatch"];
export type IncomeItemIn = Schemas["IncomeItemIn"];
export type IncomeItemPatch = Schemas["IncomeItemPatch"];
export type TaxItemIn = Schemas["TaxItemIn"];
export type TaxItemPatch = Schemas["TaxItemPatch"];

export function useTaxDocument(id: number, ownerId: OwnerId, enabled = true) {
  return useQuery({
    enabled,
    queryKey: [...ROOT_KEY, "document", id, ownerId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET(
        "/tax/documents/{document_id}",
        {
          params: { path: { document_id: id }, query: owner(ownerId) },
        },
      );
      throwIfError(error);
      if (!data) throw new Error("document not found");
      return data;
    },
  });
}

/** A mutation that refreshes every tax query (summary, years, payers, documents, details). */
function useTaxMutation<V, R>(fn: (v: V) => Promise<R>, alsoPreviews = false) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ROOT_KEY });
      if (alsoPreviews)
        void queryClient.invalidateQueries({ queryKey: PREVIEW_KEY });
    },
  });
}

function need<T>(data: T | undefined): T {
  if (data === undefined) throw new Error("The server returned no data.");
  return data;
}

export function useCreateManualDocument() {
  return useTaxMutation(
    async (body: {
      user_id: number;
      tax_year: number;
      original_name?: string | null;
    }) => {
      const { data, error } = await apiClient.POST("/tax/documents/manual", {
        body,
      });
      throwIfError(error);
      return need(data);
    },
  );
}

export function useCreateTaxForm(ownerId: OwnerId) {
  return useTaxMutation(
    async ({
      documentId,
      body,
    }: {
      documentId: number;
      body: TaxFormCreate;
    }) => {
      const { data, error } = await apiClient.POST(
        "/tax/documents/{document_id}/forms",
        {
          params: { path: { document_id: documentId }, query: owner(ownerId) },
          body,
        },
      );
      throwIfError(error);
      return need(data);
    },
  );
}

export function useUpdateTaxForm(ownerId: OwnerId) {
  return useTaxMutation(
    async ({ formId, patch }: { formId: number; patch: TaxFormPatch }) => {
      const { data, error } = await apiClient.PATCH("/tax/forms/{form_id}", {
        params: { path: { form_id: formId }, query: owner(ownerId) },
        body: patch,
      });
      throwIfError(error);
      return need(data);
    },
  );
}

export function useDeleteTaxForm(ownerId: OwnerId) {
  return useTaxMutation(async (formId: number) => {
    const { error } = await apiClient.DELETE("/tax/forms/{form_id}", {
      params: { path: { form_id: formId }, query: owner(ownerId) },
    });
    throwIfError(error);
  });
}

export function useConfirmTaxForm(ownerId: OwnerId) {
  return useTaxMutation(async (formId: number) => {
    const { data, error } = await apiClient.POST(
      "/tax/forms/{form_id}/confirm",
      {
        params: { path: { form_id: formId }, query: owner(ownerId) },
      },
    );
    throwIfError(error);
    return need(data);
  });
}

export function useCreateIncomeItem(ownerId: OwnerId) {
  return useTaxMutation(
    async ({ formId, body }: { formId: number; body: IncomeItemIn }) => {
      const { data, error } = await apiClient.POST(
        "/tax/forms/{form_id}/income-items",
        {
          params: { path: { form_id: formId }, query: owner(ownerId) },
          body,
        },
      );
      throwIfError(error);
      return need(data);
    },
  );
}

export function useUpdateIncomeItem(ownerId: OwnerId) {
  return useTaxMutation(
    async ({ id, patch }: { id: number; patch: IncomeItemPatch }) => {
      const { data, error } = await apiClient.PATCH(
        "/tax/income-items/{item_id}",
        {
          params: { path: { item_id: id }, query: owner(ownerId) },
          body: patch,
        },
      );
      throwIfError(error);
      return need(data);
    },
  );
}

export function useDeleteIncomeItem(ownerId: OwnerId) {
  return useTaxMutation(async (id: number) => {
    const { error } = await apiClient.DELETE("/tax/income-items/{item_id}", {
      params: { path: { item_id: id }, query: owner(ownerId) },
    });
    throwIfError(error);
  });
}

export function useCreateTaxItem(ownerId: OwnerId) {
  return useTaxMutation(
    async ({ formId, body }: { formId: number; body: TaxItemIn }) => {
      const { data, error } = await apiClient.POST(
        "/tax/forms/{form_id}/tax-items",
        {
          params: { path: { form_id: formId }, query: owner(ownerId) },
          body,
        },
      );
      throwIfError(error);
      return need(data);
    },
  );
}

export function useUpdateTaxItem(ownerId: OwnerId) {
  return useTaxMutation(
    async ({ id, patch }: { id: number; patch: TaxItemPatch }) => {
      const { data, error } = await apiClient.PATCH(
        "/tax/tax-items/{item_id}",
        {
          params: { path: { item_id: id }, query: owner(ownerId) },
          body: patch,
        },
      );
      throwIfError(error);
      return need(data);
    },
  );
}

export function useDeleteTaxItem(ownerId: OwnerId) {
  return useTaxMutation(async (id: number) => {
    const { error } = await apiClient.DELETE("/tax/tax-items/{item_id}", {
      params: { path: { item_id: id }, query: owner(ownerId) },
    });
    throwIfError(error);
  });
}

/** Move a document (with its forms and items) to another user. */
export function useReassignTaxDocument(ownerId: OwnerId) {
  return useTaxMutation(
    async ({ documentId, userId }: { documentId: number; userId: number }) => {
      const { data, error } = await apiClient.PATCH(
        "/tax/documents/{document_id}",
        {
          params: { path: { document_id: documentId }, query: owner(ownerId) },
          body: { user_id: userId },
        },
      );
      throwIfError(error);
      return need(data);
    },
    true,
  );
}

// ---- Reading chosen pages with the LLM -----------------------------------------------------------

export type TaxPageInfo = Schemas["TaxPageInfo"];
export type TaxLlmReadResponse = Schemas["TaxLlmReadResponse"];
export type TaxSourceResponse = Schemas["TaxSourceResponse"];

/** Per-page facts (suggested / never-sent) of a document whose PDF copy is kept. */
export function useTaxDocumentPages(
  id: number,
  ownerId: OwnerId,
  enabled: boolean,
) {
  return useQuery({
    enabled,
    queryKey: [...ROOT_KEY, "pages", id, ownerId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET(
        "/tax/documents/{document_id}/pages",
        {
          params: { path: { document_id: id }, query: owner(ownerId) },
        },
      );
      throwIfError(error);
      return Array.isArray(data) ? data : [];
    },
  });
}

/** True when `error` is the server's "the PDF isn't available, select the file again" answer. */
export function isPdfNeeded(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  const detail = error.detail;
  return (
    typeof detail === "object" &&
    detail !== null &&
    "code" in detail &&
    detail.code === "pdf_needed"
  );
}

/**
 * Re-select the original file of a document whose PDF copy is not kept. The server checks it is
 * the same file (sha256; otherwise a 422 "This isn't the same file as ...") and holds it
 * temporarily so the pages can be shown and read.
 */
export function useProvideTaxSource(ownerId: OwnerId) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      documentId,
      file,
    }: {
      documentId: number;
      file: File;
    }): Promise<TaxSourceResponse> => {
      const form = new FormData();
      form.append("file", file);
      const { data, error } = await apiClient.POST(
        "/tax/documents/{document_id}/source",
        {
          params: { path: { document_id: documentId }, query: owner(ownerId) },
          // A real FormData is passed through by openapi-fetch (same cast as the upload hooks).
          body: form as unknown as components["schemas"]["Body_provide_document_source_api_tax_documents__document_id__source_post"],
        },
      );
      throwIfError(error);
      if (!data) throw new Error("the file could not be provided");
      queryClient.setQueryData(
        [...ROOT_KEY, "pages", documentId, ownerId],
        data.pages,
      );
      return data;
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ROOT_KEY });
    },
  });
}

/** The redacted text that would be sent for `pages` (null = the automatic fallback's pages). */
export async function fetchTaxLlmPreview(
  id: number,
  ownerId: OwnerId,
  pages: number[] | null,
) {
  const { data, error } = await apiClient.GET(
    "/tax/documents/{document_id}/llm-preview",
    {
      params: {
        path: { document_id: id },
        query: {
          ...owner(ownerId),
          pages: pages === null ? undefined : pages.join(","),
        },
      },
    },
  );
  throwIfError(error);
  if (!data) throw new Error("preview returned no data");
  return data;
}

/** Send exactly `pages` to the configured LLM (explicit consent) and merge the result. */
export function useReadPagesWithLlm(ownerId: OwnerId) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      documentId,
      pages,
    }: {
      documentId: number;
      pages: number[];
    }): Promise<TaxLlmReadResponse> => {
      const { data, error } = await apiClient.POST(
        "/tax/documents/{document_id}/llm-fallback",
        {
          params: { path: { document_id: documentId }, query: owner(ownerId) },
          body: { pages },
        },
      );
      throwIfError(error);
      if (!data) throw new Error("the LLM read returned no data");
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ROOT_KEY });
      void queryClient.invalidateQueries({ queryKey: PREVIEW_KEY });
    },
  });
}

/** Does a document offer "Read pages with LLM…"? Needs a configured LLM and a file-backed row. */
export function useLlmReadAvailable(document: {
  page_count: number | null;
}): boolean {
  const mode = useSettings().data?.llm?.mode;
  return (mode === "local" || mode === "remote") && document.page_count != null;
}
