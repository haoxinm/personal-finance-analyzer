"""FastAPI application factory (§3.12, I8, A31).

`create_app()` builds the app fresh each time (one call per `serve` start, and one per test): it
wires a fresh engine/session factory, starts the job runner, generates a per-launch token, and
mounts every router before the static frontend, with an SPA fallback for unknown non-API paths.

Binding: `uvicorn.run(app, host="127.0.0.1", port=...)` in `cli.py`'s `serve` command. This module
does not itself decide the bind address; it only enforces, via `LocalhostGuardMiddleware`, that
every `/api` request's `Host` header is consistent with `127.0.0.1`/`localhost`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from spend_analyzer.api.restore_gate import RestoreGate, RestoreGateMiddleware
from spend_analyzer.api.routers import (
    accounts,
    analytics,
    backup,
    classify,
    export,
    forecast,
    issuers,
    layout_specs,
    net_worth,
    review,
    rules,
    statements,
    tax,
    taxonomy,
    transactions,
    users,
)
from spend_analyzer.api.routers import (
    jobs as jobs_router,
)
from spend_analyzer.api.routers import (
    settings as settings_router,
)
from spend_analyzer.api.security import LocalhostGuardMiddleware, generate_token
from spend_analyzer.config import Settings
from spend_analyzer.db.session import make_engine, make_session_factory
from spend_analyzer.ingest.tax import staging as tax_staging
from spend_analyzer.jobs.runner import JobRunner

#: The web/ build output is git-ignored and only present once the frontend has been built (§0.4);
#: `create_app()` mounts it if and only if it exists, so the API works standalone in dev/tests.
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(
    *,
    engine: Engine | None = None,
    settings: Settings | None = None,
    token: str | None = None,
    web_dir: Path | None = None,
) -> FastAPI:
    """Build a fresh FastAPI app. `engine` defaults to the configured data directory's database;
    tests pass a temp-file engine. `token` defaults to a fresh random per-launch token (A31)."""
    resolved_settings = settings if settings is not None else Settings()
    resolved_engine = engine if engine is not None else make_engine()
    resolved_token = token if token is not None else generate_token()
    session_factory: sessionmaker[Session] = make_session_factory(resolved_engine)

    tax_staging.purge_all()  # re-selected tax PDFs never outlive the process
    app = FastAPI(title="Spend Analyzer API", version="0.1.0")
    app.state.engine = resolved_engine
    app.state.session_factory = session_factory
    app.state.settings = resolved_settings
    app.state.token = resolved_token
    app.state.job_runner = JobRunner(session_factory)
    app.state.job_runner.start()
    app.state.restore_gate = RestoreGate()

    # Added first so it sits inside the localhost guard: unauthenticated requests are rejected
    # before a restore in progress can count (or turn away) them.
    app.add_middleware(RestoreGateMiddleware, gate=app.state.restore_gate)
    app.add_middleware(
        LocalhostGuardMiddleware, token=resolved_token, port=resolved_settings.server.port
    )

    for router in (
        users.router,
        accounts.router,
        statements.router,
        transactions.router,
        review.router,
        classify.router,
        jobs_router.router,
        analytics.router,
        taxonomy.router,
        issuers.router,
        layout_specs.router,
        rules.router,
        settings_router.router,
        export.router,
        net_worth.router,
        forecast.router,
        tax.router,
        backup.router,
    ):
        app.include_router(router, prefix="/api")

    _mount_frontend(app, web_dir if web_dir is not None else _WEB_DIR, resolved_token)
    return app


def _mount_frontend(app: FastAPI, web_dir: Path, token: str) -> None:
    """Serve the built frontend from `web_dir`, injecting the per-launch token into
    `index.html` as a `<meta name="spend-token">` tag (A31; `frontend/src/api/client.ts` reads
    it from there) and falling back to `index.html` for any unknown non-API path (SPA routing)."""
    index_path = web_dir / "index.html"
    if not web_dir.is_dir() or not index_path.is_file():

        @app.get("/", include_in_schema=False)
        def web_ui_missing() -> HTMLResponse:
            return HTMLResponse(
                "<!doctype html><title>Spend Analyzer</title><h1>Web UI not built</h1>"
                "<p>The API is running, but the web UI has not been built. From a source "
                "checkout run: <code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run "
                "build</code>, then restart <code>spend-analyzer serve</code>.</p>",
                status_code=503,
            )

        return

    assets_dir = web_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="web-assets")

    index_html = _inject_token(index_path.read_text(encoding="utf-8"), token)

    @app.get("/", include_in_schema=False)
    def serve_index() -> HTMLResponse:
        return HTMLResponse(index_html)

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    def serve_spa(full_path: str) -> HTMLResponse | FileResponse:
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="not found")
        candidate = web_dir / full_path
        if candidate.is_file() and candidate.resolve().is_relative_to(web_dir.resolve()):
            return FileResponse(candidate)
        return HTMLResponse(index_html)


def _inject_token(html: str, token: str) -> str:
    meta_tag = f'<meta name="spend-token" content="{token}">'
    if "</head>" in html:
        return html.replace("</head>", f"{meta_tag}</head>", 1)
    return meta_tag + html
