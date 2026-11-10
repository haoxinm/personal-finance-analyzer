"""`create_app()` with a built frontend present (A31): the SPA fallback route must not break
FastAPI's OpenAPI/response-model generation once `web/index.html` actually exists, and the
per-launch token must land in the served `index.html`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from personal_finance_analyzer.api.app import create_app
from personal_finance_analyzer.config import Settings


def test_create_app_succeeds_with_a_built_frontend_present(engine: Engine, tmp_path: Path) -> None:
    """Regression: building the app used to raise `FastAPIError: Invalid args for response
    field!` for the SPA fallback route as soon as `web_dir/index.html` existed, because its
    `HTMLResponse | FileResponse` return annotation has no single Pydantic response model."""
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<html><head></head><body>app</body></html>")

    app_instance = create_app(
        engine=engine, settings=Settings(), token="test-token", web_dir=web_dir
    )

    # The regression manifested at app-build time (FastAPI eagerly validates every route's
    # response model), but also confirm the OpenAPI document — every API test's `app_instance`
    # fixture calls this — still generates cleanly with the frontend mounted.
    app_instance.openapi()


def test_served_index_html_has_the_per_launch_token_injected(
    engine: Engine, tmp_path: Path
) -> None:
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<html><head><title>x</title></head><body></body></html>")

    app_instance = create_app(
        engine=engine, settings=Settings(), token="the-per-launch-token", web_dir=web_dir
    )

    with TestClient(app_instance, base_url="http://127.0.0.1") as client:
        response = client.get("/", headers={"Host": "127.0.0.1"})
        assert response.status_code == 200
        assert 'name="pfa-token" content="the-per-launch-token"' in response.text

        spa_response = client.get("/some/client-side/route", headers={"Host": "127.0.0.1"})
        assert spa_response.status_code == 200
        assert 'name="pfa-token" content="the-per-launch-token"' in spa_response.text

        # /api/* is token-protected regardless of route (I8/A31); an unknown path still 403s
        # rather than falling through to the SPA's catch-all and serving index.html for it.
        unauthenticated = client.get("/api/does-not-exist", headers={"Host": "127.0.0.1"})
        assert unauthenticated.status_code == 403

        authenticated_404 = client.get(
            "/api/does-not-exist",
            headers={"Host": "127.0.0.1", "X-PFA-Token": "the-per-launch-token"},
        )
        assert authenticated_404.status_code == 404
