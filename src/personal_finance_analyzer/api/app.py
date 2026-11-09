"""FastAPI application factory (§3.12, I8, A31).

`create_app()` builds the app fresh each time (one call per `serve` start, and one per test): it
wires a fresh engine/session factory, starts the job runner, generates a per-launch token, and
mounts every router before the static frontend, with an SPA fallback for unknown non-API paths.

Binding: `uvicorn.run(app, host=..., port=...)` in `cli.py`'s `serve` command (`127.0.0.1` unless
`--host`/`PFA_HOST` says otherwise). This module does not itself decide the bind address; it only
enforces, via `LocalhostGuardMiddleware`, that every `/api` request's `Host` header is acceptable
(`127.0.0.1`/`localhost`, plus any `allowed_hosts`), and, when given an `AuthConfig`, that a
visitor has signed in (`api/auth.py`).
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from personal_finance_analyzer.api.auth import (
    AuthConfig,
    AuthMiddleware,
    AuthState,
    register_auth_routes,
)
from personal_finance_analyzer.api.clients import (
    BlockLog,
    ClientGuardMiddleware,
    ClientPolicy,
    TrustedProxies,
    default_gateway,
)
from personal_finance_analyzer.api.restore_gate import RestoreGate, RestoreGateMiddleware
from personal_finance_analyzer.api.routers import (
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
from personal_finance_analyzer.api.routers import (
    jobs as jobs_router,
)
from personal_finance_analyzer.api.routers import (
    settings as settings_router,
)
from personal_finance_analyzer.api.security import (
    HEALTH_PATH,
    HostPolicy,
    LocalhostGuardMiddleware,
    generate_token,
)
from personal_finance_analyzer.backup import archive as backup_archive
from personal_finance_analyzer.config import Settings
from personal_finance_analyzer.core.paths import get_home, prepare_home
from personal_finance_analyzer.db.documents import prepare_documents
from personal_finance_analyzer.db.session import make_engine, make_session_factory
from personal_finance_analyzer.ingest.tax import staging as tax_staging
from personal_finance_analyzer.jobs.runner import JobRunner

#: The web/ build output is git-ignored and only present once the frontend has been built (§0.4);
#: `create_app()` mounts it if and only if it exists, so the API works standalone in dev/tests.
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(
    *,
    engine: Engine | None = None,
    settings: Settings | None = None,
    token: str | None = None,
    web_dir: Path | None = None,
    auth: AuthConfig | None = None,
    allowed_hosts: Sequence[str] = (),
    client_policy: ClientPolicy | None = None,
    trusted_proxies: TrustedProxies | None = None,
) -> FastAPI:
    """Build a fresh FastAPI app. `engine` defaults to the configured data directory's database;
    tests pass a temp-file engine. `token` defaults to a fresh random per-launch token (A31).

    `allowed_hosts` adds `Host` names to the allowlist (`api/security.py`'s `HostPolicy`), and
    `auth` puts the single-password login (`api/auth.py`) in front of everything; `pfa serve`
    passes both from its environment when it listens beyond loopback. `client_policy` refuses (403,
    before the login) every client whose address is outside it, and `trusted_proxies` names the
    reverse proxies whose `X-Forwarded-*` headers decide that address and the scheme
    (`api/clients.py`). Without any of these the app is the plain localhost one."""
    resolved_settings = settings if settings is not None else Settings()
    if engine is None:
        prepare_home()  # app startup is an explicit step: carries a pre-rename data dir/db over
        resolved_engine = make_engine()
        prepare_documents(resolved_engine)
    else:
        resolved_engine = engine
    resolved_token = token if token is not None else generate_token()
    session_factory: sessionmaker[Session] = make_session_factory(resolved_engine)

    tax_staging.purge_all()  # re-selected tax PDFs never outlive the process
    # A backup export or restore that was killed leaves plaintext scratch files behind.
    with contextlib.suppress(OSError):
        backup_archive.sweep_stale_work_dirs(get_home())
    app = FastAPI(title="Personal Finance Analyzer API", version="0.1.0")
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
        LocalhostGuardMiddleware,
        token=resolved_token,
        port=resolved_settings.server.port,
        allowed_hosts=allowed_hosts,
    )
    if auth is not None:
        # Outside the token check: an anonymous request never reaches it.
        auth_state = AuthState(auth)
        app.state.auth = auth_state
        app.add_middleware(
            AuthMiddleware,
            state=auth_state,
            policy=HostPolicy(resolved_settings.server.port, allowed_hosts),
        )
        register_auth_routes(app, auth_state)
    if client_policy is not None or trusted_proxies is not None:
        # Added after everything else, so it is the outermost layer: a client outside the local
        # network never reaches the login, and the login sees the real client address.
        app.add_middleware(
            ClientGuardMiddleware,
            policy=client_policy,
            trusted=trusted_proxies,
            block_log=BlockLog(allowed=client_policy.describe()) if client_policy else None,
            gateway=default_gateway() if client_policy else None,
        )

    @app.get(HEALTH_PATH, include_in_schema=False)
    def health() -> JSONResponse:
        """Liveness for a container healthcheck or a proxy: 200 when the database answers."""
        try:
            with resolved_engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

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

    _mount_frontend(
        app,
        web_dir if web_dir is not None else _WEB_DIR,
        resolved_token,
        signed_in_only=auth is not None,
    )
    return app


def _mount_frontend(
    app: FastAPI, web_dir: Path, token: str, *, signed_in_only: bool = False
) -> None:
    """Serve the built frontend from `web_dir`, injecting the per-launch token into
    `index.html` as a `<meta name="pfa-token">` tag (A31; `frontend/src/api/client.ts` reads
    it from there) and falling back to `index.html` for any unknown non-API path (SPA routing)."""
    index_path = web_dir / "index.html"
    if not web_dir.is_dir() or not index_path.is_file():

        @app.get("/", include_in_schema=False)
        def web_ui_missing() -> HTMLResponse:
            return HTMLResponse(
                "<!doctype html><title>Personal Finance Analyzer</title><h1>Web UI not built</h1>"
                "<p>The API is running, but the web UI has not been built. From a source "
                "checkout run: <code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run "
                "build</code>, then restart <code>pfa serve</code>.</p>",
                status_code=503,
            )

        return

    assets_dir = web_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="web-assets")

    index_html = _inject_token(
        index_path.read_text(encoding="utf-8"), token, signed_in_only=signed_in_only
    )

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


def _inject_token(html: str, token: str, *, signed_in_only: bool = False) -> str:
    """Add the token meta tag (and, behind the login, a ``pfa-auth`` tag that tells the UI to
    show a sign-out button)."""
    meta_tag = f'<meta name="pfa-token" content="{token}">'
    if signed_in_only:
        meta_tag += '<meta name="pfa-auth" content="1">'
    if "</head>" in html:
        return html.replace("</head>", f"{meta_tag}</head>", 1)
    return meta_tag + html
