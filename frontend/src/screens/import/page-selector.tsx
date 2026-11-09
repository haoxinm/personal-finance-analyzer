import * as React from "react";

import { LlmEgressNotice, LlmRequestPreview, PagePicker, type PagePickerPage } from "@/components/llm-preview";

import { fetchStatementLlmPreview, isPdfNotFound, useImportSettings, useStatementPages } from "./api";
import { pageImageUrl } from "./pfa-token";
import type { ImportItem } from "./types";
import type { Action } from "./use-import-queue";
import { isLlmChoice } from "./use-import-queue";

function LlmNotice({ item, pages, total }: { item: ImportItem; pages: number[] | null; total: number }) {
  const settings = useImportSettings();
  const mode = settings.data?.llm.mode;
  const statementId = item.statementId as number;

  return (
    <div className="flex flex-col gap-2 rounded-md border border-input p-3">
      <LlmEgressNotice
        mode={mode}
        url={settings.data?.llm.base_url ?? ""}
        masked="Account numbers and emails"
      />
      <LlmRequestPreview
        queryKey={["statement", statementId, item.statement?.user_id ?? null, pages === null ? "all" : pages.join(",")]}
        fetchPreview={() => fetchStatementLlmPreview(statementId, pages)}
        enabled={pages?.length !== 0}
        mode={mode}
        endpoint={settings.data?.llm.base_url}
        selectedPageCount={pages === null ? total : pages.length}
      />
    </div>
  );
}

export interface PageSelectorProps {
  item: ImportItem;
  dispatch: React.Dispatch<Action>;
}

/** Per-statement page selection (the shared `PagePicker`) plus the LLM privacy notice and the
 * collapsed "Show what would be sent" preview when the LLM extractor is chosen. */
export function PageSelector({ item, dispatch }: PageSelectorProps) {
  const isLlm = isLlmChoice(item.extractorChoice);
  const pageCount = item.statement?.page_count ?? null;
  const visible = item.status === "awaiting_extractor" && (isLlm || (pageCount ?? 0) > 1);
  const pagesQuery = useStatementPages(item.statementId, visible);
  const infos = pagesQuery.data;
  const total = pageCount ?? infos?.length ?? 0;
  const allPages = React.useMemo(() => Array.from({ length: total }, (_, i) => i + 1), [total]);
  const selected = item.selectedPages ?? allPages;
  const pages = React.useMemo<PagePickerPage[]>(
    () =>
      (infos ?? []).map((p) => ({
        pageNumber: p.page_number,
        charCount: p.char_count,
        title: p.snippet,
      })),
    [infos],
  );

  if (!visible || item.statementId === undefined) return null;
  const statementId = item.statementId;

  if (isPdfNotFound(pagesQuery.error)) {
    return (
      <p role="alert" className="text-sm text-destructive" data-testid="pdf-not-found">
        PDF not found. The copy of this statement is no longer in your documents folder (it was moved out or
        deleted). Put it back, or import the file again.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <PagePicker
        total={total}
        pages={pages}
        selected={selected}
        onChange={(next) =>
          dispatch({
            type: "pages_selected",
            clientId: item.clientId,
            pages: next.length === total ? null : next,
          })
        }
        imageUrl={(pageNumber, width) => pageImageUrl(statementId, pageNumber, width)}
        emptyMessage="Select at least one page to import."
      />
      {isLlm ? <LlmNotice item={item} pages={item.selectedPages ?? null} total={total} /> : null}
    </div>
  );
}
