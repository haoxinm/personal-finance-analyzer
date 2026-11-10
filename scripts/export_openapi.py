#!/usr/bin/env python3
"""Generate the real OpenAPI document from the FastAPI app and write it to
`frontend/src/api/openapi.json` (GATE 2: "Regenerate `openapi.json` and hand it to Phase 3").

Usage::

    uv run python scripts/export_openapi.py

Building the app for this alone never binds a port or starts the job runner's worker thread doing
real work: `create_app()` is only asked for its `.openapi()` schema, never run with uvicorn, and it
targets an in-memory SQLite database so it needs no `PFA_HOME`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUTPUT_PATH = _REPO_ROOT / "frontend" / "src" / "api" / "openapi.json"


def _relativize_to_api_server(schema: dict[str, object]) -> None:
    """Rewrite every path key from `/api/...` to `/...` and declare `servers: [{"url": "/api"}]`
    (the convention `frontend/src/api/client.ts` and its `openapi-fetch` client assume: a request
    for path "/users" resolves against the base URL, which already ends in `/api`). The live
    server still mounts every router under `/api` (I8/A31's Host+token middleware only guards
    that prefix) — only this exported *schema*'s paths are relativized."""
    paths = schema.get("paths")
    if not isinstance(paths, dict):  # pragma: no cover - defensive
        return
    schema["paths"] = {
        (key[len("/api") :] if key.startswith("/api") else key): value
        for key, value in paths.items()
    }
    schema["servers"] = [{"url": "/api"}]


def main() -> int:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from personal_finance_analyzer.api.app import create_app
    from personal_finance_analyzer.db.session import make_engine_for_path

    engine = make_engine_for_path(Path(":memory:"))
    try:
        app = create_app(engine=engine, token="export-openapi-placeholder")
        schema = app.openapi()
    finally:
        engine.dispose()

    _relativize_to_api_server(schema)

    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_PATH.write_text(json.dumps(schema, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {_OUTPUT_PATH.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
