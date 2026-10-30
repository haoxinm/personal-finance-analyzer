"""Shared fixtures for `tests/api/**`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from spend_analyzer.api.app import create_app
from spend_analyzer.config import Settings

TEST_TOKEN = "test-token"


@pytest.fixture
def app_instance(engine: Engine, session: Session, tmp_path: Path) -> FastAPI:
    """Depends on the `session` fixture (not just `engine`) so the taxonomy is synced before any
    route runs, even for a test that never asks for `session` itself. The web directory points at
    nothing, so a locally built frontend (`src/spend_analyzer/web/`, whose SPA fallback answers
    every unknown GET) cannot change what the API routes return."""
    del session
    return create_app(
        engine=engine, settings=Settings(), token=TEST_TOKEN, web_dir=tmp_path / "no-web"
    )


@pytest.fixture
def client(app_instance: FastAPI) -> Iterator[TestClient]:
    with TestClient(
        app_instance,
        base_url="http://127.0.0.1",
        headers={"Host": "127.0.0.1", "X-Spend-Token": TEST_TOKEN},
    ) as test_client:
        yield test_client
