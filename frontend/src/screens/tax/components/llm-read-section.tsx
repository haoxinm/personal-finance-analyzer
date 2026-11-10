import * as React from "react";

import { useSettings } from "@/api/hooks";
import {
  LlmEgressNotice,
  LlmRequestPreview,
  PagePicker,
  hostOf,
  type PagePickerPage,
} from "@/components/llm-preview";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/use-toast";
import { FileDropZone } from "@/components/primitives/file-drop-zone";
import { taxPageImageUrl } from "@/screens/import/pfa-token";

import {
  fetchTaxLlmPreview,
  isPdfNeeded,
  useProvideTaxSource,
  useReadPagesWithLlm,
  useTaxDocumentPages,
} from "../hooks";
import { LLM_READ_MASKED, errorMessage, type TaxDocument } from "../logic";

/**
 * Per-document, explicit LLM read: choose pages (preselected = what the automatic fallback would
 * send), see exactly the redacted text (collapsed until asked for), then confirm to send them.
 * Allowed whenever an LLM is configured, whatever the automatic tax setting says.
 */
export function LlmReadSection({ document: d }: { document: TaxDocument }) {
  const settings = useSettings();
  const { toast } = useToast();
  const mode = settings.data?.llm?.mode;
  const endpoint = settings.data?.llm?.base_url ?? "";
  // The PDF is either kept (`has_pdf`) or was re-selected by the user and is held temporarily.
  const hasPdf = d.has_pdf || d.staged_until != null;
  const pagesQuery = useTaxDocumentPages(d.id, d.user_id, hasPdf);
  const provide = useProvideTaxSource(d.user_id);
  const read = useReadPagesWithLlm(d.user_id);
  const expired = hasPdf && !d.has_pdf && isPdfNeeded(pagesQuery.error);
  const [chosen, setChosen] = React.useState<number[] | null>(null);
  const [confirming, setConfirming] = React.useState(false);
  // True while the expanded preview for the current selection is loading: sending is blocked so
  // the text shown is always the text that would be sent.
  const [previewPending, setPreviewPending] = React.useState(false);

  const infos = pagesQuery.data;
  const suggested = React.useMemo(
    () => (infos ?? []).filter((p) => p.suggested).map((p) => p.page_number),
    [infos],
  );
  const selected = chosen ?? suggested;
  const total = d.page_count ?? infos?.length ?? 0;
  const pickerPages = React.useMemo<PagePickerPage[]>(
    () =>
      (infos ?? []).map((p) => ({
        pageNumber: p.page_number,
        charCount: p.char_count,
        disabledReason: p.excluded_reason,
        suggested: p.suggested,
      })),
    [infos],
  );

  async function onFiles(files: File[]) {
    const file = files[0];
    if (!file) return;
    try {
      await provide.mutateAsync({ documentId: d.id, file });
      setChosen(null);
    } catch (e) {
      toast({
        variant: "destructive",
        title: "Could not use that file",
        description: errorMessage(e),
      });
    }
  }

  if (!hasPdf || expired) {
    return (
      <div
        className="flex flex-col gap-2 rounded-md border border-input p-3"
        data-testid="llm-read-section"
      >
        {d.pdf_missing ? (
          <p role="alert" className="text-xs text-destructive" data-testid="pdf-not-found">
            PDF not found. The copy of this document is no longer in your
            documents folder (it was moved out or deleted). Put it back, or
            select the original file again below to read its pages.
          </p>
        ) : null}
        <p className="text-xs text-muted-foreground">
          {expired ? "The temporary copy has expired. " : null}
          To read pages with your LLM, select the original file again
          {d.pdf_missing ? "" : " (it isn’t kept after import for privacy)"}. It
          is held only for this session’s need (about 30 minutes, or until the
          pages are sent) and checked to be the same file.
        </p>
        <FileDropZone
          onFiles={(files) => void onFiles(files)}
          disabled={provide.isPending}
          title={`Drop ${d.original_name} here, or click to browse`}
          hint="It stays on this computer; only the pages you confirm are sent, redacted."
          inputLabel={`Select ${d.original_name} again`}
        />
        {provide.isPending ? (
          <span role="status" className="text-xs text-muted-foreground">
            Checking the file…
          </span>
        ) : null}
      </div>
    );
  }

  async function onSend() {
    setConfirming(false);
    try {
      const r = await read.mutateAsync({ documentId: d.id, pages: selected });
      const stored = r.inserted_forms + r.merged_forms;
      toast({
        variant: stored > 0 ? "success" : "default",
        title:
          stored > 0
            ? `Read ${stored} form${stored === 1 ? "" : "s"} with the LLM`
            : "The LLM found nothing new",
        description:
          stored > 0
            ? "Review the figures marked “Read by LLM — needs review”."
            : (r.warnings[0] ?? "No new figures were stored."),
      });
      setChosen(null);
    } catch (e) {
      toast({
        variant: "destructive",
        title: "Could not read the pages",
        description: errorMessage(e),
      });
    }
  }

  return (
    <div
      className="flex flex-col gap-3 rounded-md border border-input p-3"
      data-testid="llm-read-section"
    >
      <p className="text-xs text-muted-foreground">
        Pick the pages the LLM should read. Nothing is sent until you confirm;
        Form 1040 pages are never sent.
      </p>
      {pagesQuery.isPending ? (
        <p className="text-xs text-muted-foreground">Loading pages…</p>
      ) : null}
      {pagesQuery.isError ? (
        <div className="flex flex-wrap items-center gap-2">
          <p role="alert" className="text-xs text-destructive">
            Could not load the pages: {errorMessage(pagesQuery.error)}
          </p>
          <Button size="sm" variant="outline" onClick={() => void pagesQuery.refetch()}>
            Try again
          </Button>
        </div>
      ) : null}
      {infos ? (
        <PagePicker
          total={total}
          pages={pickerPages}
          selected={selected}
          onChange={setChosen}
          imageUrl={(page, width) =>
            taxPageImageUrl(d.id, page, width, d.user_id)
          }
          emptyMessage="Select at least one page to read."
        />
      ) : null}
      <LlmEgressNotice mode={mode} url={endpoint} masked={LLM_READ_MASKED} />
      <LlmRequestPreview
        queryKey={["tax", d.id, d.user_id, selected.join(",")]}
        onPendingChange={setPreviewPending}
        fetchPreview={() => fetchTaxLlmPreview(d.id, d.user_id, selected)}
        enabled={selected.length > 0}
        mode={mode}
        endpoint={endpoint}
        selectedPageCount={selected.length}
      />
      <div className="flex flex-wrap items-center gap-2">
        {confirming ? (
          <>
            <span className="text-xs">
              Send the redacted text of {selected.length} page
              {selected.length === 1 ? "" : "s"} to{" "}
              {mode === "local" ? "your local model" : hostOf(endpoint)}?
            </span>
            <Button
              size="sm"
              disabled={read.isPending || previewPending}
              onClick={() => void onSend()}
            >
              Confirm send
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setConfirming(false)}
            >
              Cancel send
            </Button>
          </>
        ) : (
          <Button
            size="sm"
            disabled={selected.length === 0 || read.isPending || previewPending}
            onClick={() => setConfirming(true)}
          >
            Send selected pages to LLM
          </Button>
        )}
        {read.isPending ? (
          <span role="status" className="text-xs text-muted-foreground">
            Waiting for the LLM…
          </span>
        ) : null}
      </div>
    </div>
  );
}
