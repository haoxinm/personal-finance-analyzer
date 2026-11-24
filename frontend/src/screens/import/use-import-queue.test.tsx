import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { setSkipConfirmForIssuer } from "./storage";
import type { ExtractorProposal, LayoutSpec, Statement } from "./types";
import {
  CONFIDENT_SCORE_THRESHOLD,
  extractorChoiceForIssuer,
  isConfidentProposal,
  pendingPeers,
  shouldAutoConfirm,
  useImportQueue,
} from "./use-import-queue";
import type { ImportItem } from "./types";

function makeStatement(overrides: Partial<Statement> = {}): Statement {
  return {
    id: 1,
    user_id: 1,
    account_id: 5,
    status: "awaiting_extractor",
    period_start: "2024-01-01",
    period_end: "2024-01-31",
    txn_count: null,
    reconciliation_delta_minor: null,
    detect_score: 0.9,
    shape_warnings: null,
    error_detail: null,
    created_at: "2024-02-01T00:00:00Z",
    original_name: "statement.pdf",
    has_pdf: true,
    pdf_in_place: false,
    pdf_missing: false,
    flip_sign: false,
    period_corrected: false,
    manual_count: 0,
    excluded_count: 0,
    sign_flip_suggested: false,
    ...overrides,
  };
}

function makeProposal(overrides: Partial<ExtractorProposal> = {}): ExtractorProposal {
  return {
    issuer_id: 3,
    parser_id: "layout_a_credit",
    layout_spec_id: null,
    confidence: 0.9,
    auto_confirmed: false,
    remembered: false,
    ...overrides,
  };
}

function makeItem(overrides: Partial<ImportItem> = {}): ImportItem {
  return {
    clientId: "c1",
    fileName: "statement.pdf",
    userId: 1,
    status: "awaiting_extractor",
    issuerId: 3,
    extractorChoice: { kind: "parser", parserId: "layout_a_credit" },
    remember: true,
    proposal: makeProposal(),
    ...overrides,
  };
}

describe("useImportQueue", () => {
  it("starts empty and queues files as uploading items with a stable per-file clientId", () => {
    const { result } = renderHook(() => useImportQueue());
    expect(result.current.items).toEqual([]);

    let clientIds: string[] = [];
    act(() => {
      clientIds = result.current.queueFiles(
        [new File(["a"], "a.pdf"), new File(["b"], "b.pdf")],
        1,
      );
    });

    expect(clientIds).toHaveLength(2);
    expect(new Set(clientIds).size).toBe(2);
    expect(result.current.items).toHaveLength(2);
    for (const item of result.current.items) {
      expect(item.status).toBe("uploading");
      expect(item.userId).toBe(1);
    }
  });

  it("moves an uploaded item to awaiting_extractor and pre-selects the proposal", () => {
    const { result } = renderHook(() => useImportQueue());
    let clientId = "";
    act(() => {
      clientId = result.current.queueFiles([new File(["a"], "a.pdf")], 1)[0] ?? "";
    });

    const statement = makeStatement();
    const proposal = makeProposal();
    act(() => {
      result.current.dispatch({ type: "upload_succeeded", clientId, statement, proposal });
    });

    const item = result.current.items[0];
    expect(item?.status).toBe("awaiting_extractor");
    expect(item?.issuerId).toBe(3);
    expect(item?.extractorChoice).toEqual({ kind: "parser", parserId: "layout_a_credit" });
  });

  it("prefers a remembered layout spec over the built-in parser guess when both are proposed", () => {
    const { result } = renderHook(() => useImportQueue());
    let clientId = "";
    act(() => {
      clientId = result.current.queueFiles([new File(["a"], "a.pdf")], 1)[0] ?? "";
    });

    act(() => {
      result.current.dispatch({
        type: "upload_succeeded",
        clientId,
        statement: makeStatement(),
        proposal: makeProposal({ layout_spec_id: 99 }),
      });
    });

    expect(result.current.items[0]?.extractorChoice).toEqual({ kind: "spec", layoutSpecId: 99 });
  });

  it("routes a no-text-layer upload straight to that terminal status with its message", () => {
    const { result } = renderHook(() => useImportQueue());
    let clientId = "";
    act(() => {
      clientId = result.current.queueFiles([new File(["a"], "a.pdf")], 1)[0] ?? "";
    });

    act(() => {
      result.current.dispatch({
        type: "upload_succeeded",
        clientId,
        statement: makeStatement({ status: "no_text_layer", error_detail: "Can't read this PDF" }),
        proposal: makeProposal(),
      });
    });

    expect(result.current.items[0]?.status).toBe("no_text_layer");
    expect(result.current.items[0]?.errorMessage).toBe("Can't read this PDF");
  });

  it("routes a byte-identical re-upload (already parsed) straight to parsed", () => {
    const { result } = renderHook(() => useImportQueue());
    let clientId = "";
    act(() => {
      clientId = result.current.queueFiles([new File(["a"], "a.pdf")], 1)[0] ?? "";
    });

    act(() => {
      result.current.dispatch({
        type: "upload_succeeded",
        clientId,
        statement: makeStatement({ status: "parsed", txn_count: 12 }),
        proposal: makeProposal(),
      });
    });

    expect(result.current.items[0]?.status).toBe("parsed");
  });

  it("records upload failures", () => {
    const { result } = renderHook(() => useImportQueue());
    let clientId = "";
    act(() => {
      clientId = result.current.queueFiles([new File(["a"], "a.pdf")], 1)[0] ?? "";
    });

    act(() => {
      result.current.dispatch({ type: "upload_failed", clientId, message: "network error" });
    });

    expect(result.current.items[0]?.status).toBe("upload_failed");
    expect(result.current.items[0]?.errorMessage).toBe("network error");
  });

  it("tracks issuer/extractor overrides and the remember toggle independently per item", () => {
    const { result } = renderHook(() => useImportQueue());
    let clientId = "";
    act(() => {
      clientId = result.current.queueFiles([new File(["a"], "a.pdf")], 1)[0] ?? "";
    });
    act(() => {
      result.current.dispatch({
        type: "upload_succeeded",
        clientId,
        statement: makeStatement(),
        proposal: makeProposal(),
      });
    });

    act(() => {
      result.current.dispatch({
        type: "issuer_selected",
        clientId,
        issuerId: 8,
        extractorChoice: { kind: "spec", layoutSpecId: 55 },
      });
    });
    expect(result.current.items[0]?.issuerId).toBe(8);
    expect(result.current.items[0]?.extractorChoice).toEqual({ kind: "spec", layoutSpecId: 55 });

    act(() => {
      result.current.dispatch({
        type: "extractor_selected",
        clientId,
        extractorChoice: { kind: "parser", parserId: "generic_table" },
      });
    });
    expect(result.current.items[0]?.extractorChoice).toEqual({
      kind: "parser",
      parserId: "generic_table",
    });

    act(() => {
      result.current.dispatch({ type: "remember_toggled", clientId, remember: false });
    });
    expect(result.current.items[0]?.remember).toBe(false);
  });

  it("moves to extracting on extract_started and back to awaiting_extractor with a message on extract_failed", () => {
    const { result } = renderHook(() => useImportQueue());
    let clientId = "";
    act(() => {
      clientId = result.current.queueFiles([new File(["a"], "a.pdf")], 1)[0] ?? "";
    });
    act(() => {
      result.current.dispatch({
        type: "upload_succeeded",
        clientId,
        statement: makeStatement(),
        proposal: makeProposal(),
      });
    });

    act(() => {
      result.current.dispatch({ type: "extract_started", clientId, jobId: "job-1" });
    });
    expect(result.current.items[0]?.status).toBe("extracting");
    expect(result.current.items[0]?.jobId).toBe("job-1");

    act(() => {
      result.current.dispatch({ type: "extract_failed", clientId, message: "boom" });
    });
    expect(result.current.items[0]?.status).toBe("awaiting_extractor");
    expect(result.current.items[0]?.errorMessage).toBe("boom");
  });

  it("applies a statement_refreshed action's status and a following needs-review count", () => {
    const { result } = renderHook(() => useImportQueue());
    let clientId = "";
    act(() => {
      clientId = result.current.queueFiles([new File(["a"], "a.pdf")], 1)[0] ?? "";
    });
    act(() => {
      result.current.dispatch({ type: "extract_started", clientId, jobId: "job-1" });
    });

    act(() => {
      result.current.dispatch({
        type: "statement_refreshed",
        clientId,
        statement: makeStatement({ status: "unsupported_layout", error_detail: "no parser matched" }),
      });
    });
    // A failed parse is a retryable warning, not a terminal state: the item goes back to
    // awaiting_extractor (picker + Import stay available) with the detail kept in `failure`.
    expect(result.current.items[0]?.status).toBe("awaiting_extractor");
    expect(result.current.items[0]?.failure).toEqual({ detail: "no parser matched", llm: false });
    expect(result.current.items[0]?.errorMessage).toBeUndefined();

    act(() => {
      result.current.dispatch({ type: "needs_review_counted", clientId, count: 4 });
    });
    expect(result.current.items[0]?.needsReviewCount).toBe(4);
  });
});

describe("built-in parser failure", () => {
  function failedItem(llmConfigured: boolean, attemptLlm = false) {
    const { result } = renderHook(() => useImportQueue());
    let clientId = "";
    act(() => {
      clientId = result.current.queueFiles([new File(["a"], "a.pdf")], 1)[0] ?? "";
    });
    act(() => {
      result.current.dispatch({
        type: "upload_succeeded",
        clientId,
        statement: makeStatement(),
        proposal: makeProposal(),
      });
    });
    if (attemptLlm) {
      act(() => {
        result.current.dispatch({ type: "extractor_selected", clientId, extractorChoice: { kind: "parser", parserId: "llm" } });
      });
    }
    act(() => {
      result.current.dispatch({ type: "extract_started", clientId, jobId: "job-1" });
    });
    act(() => {
      result.current.dispatch({
        type: "statement_refreshed",
        clientId,
        statement: makeStatement({ status: "error", error_detail: "boom" }),
        llmConfigured,
      });
    });
    return result.current.items[0];
  }

  it("preselects the LLM (without importing) when one is configured", () => {
    const item = failedItem(true);
    expect(item?.status).toBe("awaiting_extractor");
    expect(item?.extractorChoice).toEqual({ kind: "parser", parserId: "llm" });
  });

  it("keeps the proposed parser when no LLM is configured", () => {
    expect(failedItem(false)?.extractorChoice?.kind).toBe("parser");
    expect(failedItem(false)?.extractorChoice).not.toEqual({ kind: "parser", parserId: "llm" });
  });

  it("flags a failure of the LLM attempt itself as such", () => {
    expect(failedItem(true, true)?.failure).toEqual({ detail: "boom", llm: true });
  });

  it("never auto-confirms an item whose built-in parse already failed", () => {
    setSkipConfirmForIssuer(3, true);
    expect(shouldAutoConfirm(makeItem({ failure: { detail: "x", llm: false } }))).toBe(false);
    window.localStorage.clear();
  });
});

describe("isConfidentProposal / shouldAutoConfirm", () => {
  afterEach(() => {
    window.localStorage.clear();
  });

  it("is confident exactly at and above the threshold, with an issuer and extractor chosen", () => {
    expect(isConfidentProposal(makeItem({ proposal: makeProposal({ confidence: CONFIDENT_SCORE_THRESHOLD }) }))).toBe(
      true,
    );
    expect(
      isConfidentProposal(
        makeItem({ proposal: makeProposal({ confidence: CONFIDENT_SCORE_THRESHOLD - 0.01 }) }),
      ),
    ).toBe(false);
  });

  it("is never confident without a resolved issuer or extractor, regardless of the score", () => {
    expect(isConfidentProposal(makeItem({ issuerId: null }))).toBe(false);
    expect(isConfidentProposal(makeItem({ extractorChoice: null }))).toBe(false);
  });

  it("auto-confirms only a confident proposal from an issuer marked don't-ask-again", () => {
    const item = makeItem();
    expect(shouldAutoConfirm(item)).toBe(false);

    setSkipConfirmForIssuer(3, true);
    expect(shouldAutoConfirm(item)).toBe(true);

    expect(shouldAutoConfirm(makeItem({ proposal: makeProposal({ confidence: 0.5 }) }))).toBe(false);
  });
});

describe("extractorChoiceForIssuer", () => {
  const specs: LayoutSpec[] = [
    { id: 1, name: "Chase v1", version: 1, issuer_id: 3, approved: true, source: "builtin" },
    { id: 2, name: "Chase v2 draft", version: 2, issuer_id: 3, approved: false, source: "user" },
  ];

  it("prefers the issuer's own approved spec", () => {
    expect(extractorChoiceForIssuer(3, specs, "generic_table")).toEqual({
      kind: "spec",
      layoutSpecId: 1,
    });
  });

  it("falls back to the built-in parser guess when the issuer has no approved spec", () => {
    expect(extractorChoiceForIssuer(9, specs, "generic_table")).toEqual({
      kind: "parser",
      parserId: "generic_table",
    });
  });

  it("returns null when neither is available", () => {
    expect(extractorChoiceForIssuer(9, specs, null)).toBeNull();
  });
});

describe("re-proposing pending items (remember reaches the batch)", () => {
  const remembered = makeProposal({
    issuer_id: 3,
    parser_id: null,
    layout_spec_id: 9,
    confidence: 0.85,
    remembered: true,
  });

  function setup(items: ImportItem[]) {
    const hook = renderHook(() => useImportQueue());
    act(() => hook.result.current.dispatch({ type: "files_queued", items }));
    return hook;
  }
  const pending = (clientId: string, overrides: Partial<ImportItem> = {}) =>
    makeItem({ clientId, statementId: Number(clientId.slice(1)), statement: makeStatement(), ...overrides });

  it("an untouched peer adopts the new proposal and keeps its pages, remember flag and failure", () => {
    const { result } = setup([
      pending("c2", {
        issuerId: null,
        extractorChoice: null,
        selectedPages: [2, 3],
        remember: false,
        failure: { detail: "boom", llm: false, previous: true },
        proposal: makeProposal({ issuer_id: null, parser_id: null, confidence: 0 }),
      }),
    ]);
    act(() =>
      result.current.dispatch({
        type: "proposal_refreshed",
        clientId: "c2",
        statement: makeStatement({ id: 2 }),
        proposal: remembered,
      }),
    );
    const item = result.current.items[0]!;
    expect(item.issuerId).toBe(3);
    expect(item.extractorChoice).toEqual({ kind: "spec", layoutSpecId: 9 });
    expect(item.proposal).toEqual(remembered);
    expect(item.selectedPages).toEqual([2, 3]);
    expect(item.remember).toBe(false);
    expect(item.failure).toEqual({ detail: "boom", llm: false, previous: true });
    expect(item.suggestedChoice).toBeUndefined();
  });

  it("a touched peer keeps its choice and only gets a suggestion, which 'Use it' applies", () => {
    const { result } = setup([pending("c2", { choiceTouched: true, extractorChoice: { kind: "parser", parserId: "llm" } })]);
    act(() =>
      result.current.dispatch({
        type: "proposal_refreshed",
        clientId: "c2",
        statement: makeStatement({ id: 2 }),
        proposal: remembered,
      }),
    );
    let item = result.current.items[0]!;
    expect(item.extractorChoice).toEqual({ kind: "parser", parserId: "llm" });
    expect(item.suggestedChoice?.extractorChoice).toEqual({ kind: "spec", layoutSpecId: 9 });

    act(() => result.current.dispatch({ type: "suggestion_applied", clientId: "c2" }));
    item = result.current.items[0]!;
    expect(item.extractorChoice).toEqual({ kind: "spec", layoutSpecId: 9 });
    expect(item.suggestedChoice).toBeUndefined();
    expect(item.choiceTouched).toBe(false);
  });

  it("a touched peer is not nagged by a proposal that is not remembered or matches its choice", () => {
    const { result } = setup([pending("c2", { choiceTouched: true })]);
    act(() =>
      result.current.dispatch({
        type: "proposal_refreshed",
        clientId: "c2",
        statement: makeStatement({ id: 2 }),
        proposal: makeProposal({ parser_id: "other_parser", remembered: false }),
      }),
    );
    expect(result.current.items[0]!.suggestedChoice).toBeUndefined();
  });

  it("ignores items that are no longer awaiting an extractor, and picking by hand marks the item touched", () => {
    const { result } = setup([pending("c2", { status: "extracting" }), pending("c3")]);
    act(() =>
      result.current.dispatch({
        type: "proposal_refreshed",
        clientId: "c2",
        statement: makeStatement({ id: 2 }),
        proposal: remembered,
      }),
    );
    expect(result.current.items[0]!.extractorChoice).toEqual({ kind: "parser", parserId: "layout_a_credit" });
    act(() =>
      result.current.dispatch({ type: "extractor_selected", clientId: "c3", extractorChoice: { kind: "parser", parserId: "llm" } }),
    );
    expect(result.current.items[1]!.choiceTouched).toBe(true);
    act(() => result.current.dispatch({ type: "issuer_selected", clientId: "c2", issuerId: 3, extractorChoice: null }));
    expect(result.current.items[0]!.choiceTouched).toBe(true);
  });

  it("pendingPeers picks other awaiting items of the same issuer or with none", () => {
    const items = [
      pending("c1"),
      pending("c2"),
      pending("c3", { issuerId: null }),
      pending("c4", { issuerId: 8 }),
      pending("c5", { status: "extracting" }),
    ];
    expect(pendingPeers(items, "c1", 3).map((i) => i.clientId)).toEqual(["c2", "c3"]);
  });
});
