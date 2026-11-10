# Spend Analyzer — frontend

Vite + React 18 + TypeScript. Owned by work package P1-F (`frontend/**`, except
`frontend/src/screens/**`, which Phase 3 owns per screen).

## Commands

```
npm install
npm run dev          # Vite dev server; proxies /api -> http://127.0.0.1:8756
npm run build        # emits to ../src/spend_analyzer/web/ (served by FastAPI in production)
npm run typecheck
npm run lint
npm run test
npm run gen:api-types # regenerate src/api/schema.d.ts from src/api/openapi.json
```

## Layout

- `src/api/openapi.json` — a hand-written stand-in for the FastAPI-generated schema
  (plan §3.12), committed because Phase 1 has no backend yet. **P2-C replaces this file**
  with the real generated schema, at which point `gen:api-types` becomes a CI step instead
  of a manual one.
- `src/api/client.ts` — the typed client (`openapi-fetch`, generated types), plus
  `throwIfError` for TanStack Query hooks.
- `src/api/hooks.ts` — a small set of example query/mutation hooks; Phase 3 screens add
  their own directly against `apiClient` rather than growing this file per endpoint.
- `src/components/ui/` — shadcn-style primitives (Button, Badge, Card, Input, Skeleton,
  Toast).
- `src/components/primitives/` — the shared domain primitives every screen needs: `Money`,
  `CategoryBadge`, `DateRangePicker`, loading/empty/error states, `ErrorBoundary`.
- `src/hooks/` — `useSpendQueryParams` (URL search params ⇄ `SpendQuery`) and
  `useJobProgress` (SSE with reconnect).
- `src/routes/gallery.tsx` — the Storybook-less component gallery at `/gallery`.
- `src/navigation.ts` — the app's information architecture: sections (Spending, Income & Tax,
  Net worth, Settings), their sub-tabs, and per-route chrome (user bar, compare, notes, document
  title). The shell renders the top nav and the active section's sub-tab row from it. Old flat
  paths (`/review`, `/import`, ...) redirect to their `/spending/...` equivalents, keeping the
  query string.
