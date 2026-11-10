import type { Category, Transaction } from "./types";

export function makeTransaction(overrides: Partial<Transaction> & { id: number }): Transaction {
  return {
    statement_id: 1,
    account_id: 1,
    posted_date: "2026-01-01",
    transaction_date: null,
    description_raw: `RAW MERCHANT ${overrides.id}`,
    description_clean: `Merchant ${overrides.id}`,
    amount_minor: 1000,
    currency: "USD",
    kind: "purchase",
    category_key: "others",
    subcategory_key: "uncategorized",
    merchant_key: `merchant-${overrides.id}`,
    notes: null,
    needs_review: true,
    ...overrides,
  };
}

export function makeCategory(overrides: Partial<Category> & { key: string }): Category {
  return {
    name: overrides.key,
    subcategories: [],
    ...overrides,
  };
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * A `fetch` stub covering every endpoint this screen calls: `GET /transactions` (§3.12,
 * paginated the way the real backend does), `POST /review/accept` and `/review/undo`, settings,
 * `POST /classify/run` and its job, and `GET /taxonomy` plus its subcategory
 * `approve`/`merge` actions. Pass only the state a given test needs; unhandled requests throw,
 * so a test that reaches an unmocked call fails loudly instead of hanging.
 */
export interface AcceptBody {
  transaction_id: number;
  category_key: string;
  subcategory_key: string;
  create_rule: boolean;
  similar_ids: number[];
  kind?: string;
}

export function createApiFetchMock(state: {
  transactions?: Transaction[];
  categories?: Category[];
  /** Override `POST /review/accept` (default: applies to every requested `similar_ids` row that
   *  is a pending row of the same merchant, like the real endpoint). */
  acceptBehavior?: (body: AcceptBody) => Response;
  /** Override the body of `POST /review/undo` (default: restores every transaction in the token). */
  undoBehavior?: (body: { transactions: unknown[] }) => {
    restored: number;
    skipped: number[];
    rule_skipped: boolean;
  };
  settings?: { llm: { mode: string } };
  /** What `GET /transactions?uncategorized=true` reports as `total`. */
  uncategorizedTotal?: number;
  /** `POST /classify/run` replies with this many transactions to reconsider (default 3). */
  reclassifyCount?: number;
  approveBehavior?: (subcategoryId: number) => Response;
  mergeBehavior?: (subcategoryId: number, intoId: number) => Response;
  users?: Array<{ id: number; name: string; is_default: boolean }>;
}) {
  const transactions = state.transactions ?? [];
  let categories = state.categories ?? [];

  return async (input: Request | string | URL): Promise<Response> => {
    const request = input instanceof Request ? input : new Request(input);
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/api/users") {
      return jsonResponse(state.users ?? [{ id: 1, name: "Alex", is_default: true }]);
    }

    if (request.method === "GET" && url.pathname === "/api/transactions") {
      const page = Number(url.searchParams.get("page") ?? "1");
      const pageSize = Number(url.searchParams.get("page_size") ?? "50");
      const needsReview = url.searchParams.get("needs_review");
      const filtered =
        needsReview === "true" ? transactions.filter((txn) => txn.needs_review) : transactions;
      const start = (page - 1) * pageSize;
      return jsonResponse({
        items: filtered.slice(start, start + pageSize),
        total:
          url.searchParams.get("uncategorized") === "true" && state.uncategorizedTotal !== undefined
            ? state.uncategorizedTotal
            : filtered.length,
      });
    }

    if (request.method === "POST" && url.pathname === "/api/review/accept") {
      // Clone before reading: the caller may want to inspect the request body afterwards
      // (`fetchMock.mock.calls`), and a `Request`'s body stream can only be read once.
      const body = (await request.clone().json()) as AcceptBody;
      if (state.acceptBehavior) return state.acceptBehavior(body);
      const accepted = transactions.find((txn) => txn.id === body.transaction_id);
      const applied = transactions.filter(
        (txn) =>
          body.similar_ids.includes(txn.id) &&
          txn.needs_review &&
          accepted !== undefined &&
          txn.merchant_key === accepted.merchant_key,
      );
      const stateOf = (txn: Transaction, needsReview: boolean) => ({
        category_id: null,
        subcategory_id: null,
        confidence: null,
        classified_by: null,
        needs_review: needsReview,
        kind: txn.kind,
        is_spend: true,
        merchant_canonical: null,
      });
      const snapshot = (txn: Transaction) => ({
        transaction_id: txn.id,
        before: stateOf(txn, txn.needs_review),
        after: stateOf(txn, false),
      });
      const primary = accepted ?? makeTransaction({ id: body.transaction_id });
      return jsonResponse({
        transaction: {
          ...primary,
          needs_review: false,
          category_key: body.category_key,
          subcategory_key: body.subcategory_key,
        },
        similar_ids: applied.map((txn) => txn.id),
        undo: {
          transactions: [primary, ...applied].map(snapshot),
          rule: null,
        },
      });
    }

    if (request.method === "POST" && url.pathname === "/api/review/undo") {
      const body = (await request.clone().json()) as { transactions: unknown[] };
      return jsonResponse(
        state.undoBehavior?.(body) ?? { restored: body.transactions.length, skipped: [], rule_skipped: false },
      );
    }

    if (request.method === "GET" && url.pathname === "/api/settings") {
      return jsonResponse(state.settings ?? { llm: { mode: "remote" } });
    }

    if (request.method === "POST" && url.pathname === "/api/classify/run") {
      return jsonResponse(
        { job_id: "job-reclassify", group_id: "g", transaction_count: state.reclassifyCount ?? 3 },
        202,
      );
    }

    if (request.method === "GET" && url.pathname === "/api/jobs/job-reclassify") {
      return jsonResponse({ id: "job-reclassify", kind: "classify", status: "done", progress: 1 });
    }

    if (request.method === "GET" && url.pathname === "/api/taxonomy") {
      return jsonResponse(categories);
    }

    const approveMatch = /^\/api\/taxonomy\/subcategories\/(\d+)\/approve$/.exec(url.pathname);
    if (request.method === "POST" && approveMatch) {
      const subcategoryId = Number(approveMatch[1]);
      if (state.approveBehavior) return state.approveBehavior(subcategoryId);
      categories = categories.map((cat) => ({
        ...cat,
        subcategories: cat.subcategories.map((sub) =>
          "id" in sub && sub.id === subcategoryId ? { ...sub, pending: false } : sub,
        ),
      }));
      const approved = categories.flatMap((cat) => cat.subcategories).find((sub) => "id" in sub && sub.id === subcategoryId);
      return jsonResponse(approved);
    }

    const mergeMatch = /^\/api\/taxonomy\/subcategories\/(\d+)\/merge$/.exec(url.pathname);
    if (request.method === "POST" && mergeMatch) {
      const subcategoryId = Number(mergeMatch[1]);
      const body = (await request.clone().json()) as { into_id: number };
      if (state.mergeBehavior) return state.mergeBehavior(subcategoryId, body.into_id);
      categories = categories.map((cat) => ({
        ...cat,
        subcategories: cat.subcategories.map((sub) =>
          "id" in sub && sub.id === subcategoryId
            ? { ...sub, merged_into: String(body.into_id) }
            : sub,
        ),
      }));
      const merged = categories.flatMap((cat) => cat.subcategories).find((sub) => "id" in sub && sub.id === subcategoryId);
      return jsonResponse(merged);
    }

    throw new Error(`unhandled request in test: ${request.method} ${url.pathname}`);
  };
}
