"""FastAPI dependencies: one `Session` per request, and access to the running app's settings and
job runner (§3.2: routes that touch the DB are plain `def`, so Starlette runs them in its
threadpool against the synchronous engine/session set up in `api/app.py`)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from personal_finance_analyzer.config import Settings, load_settings
from personal_finance_analyzer.jobs.runner import JobRunner


def get_session_factory(request: Request) -> sessionmaker[Session]:
    """Return this app's session factory, for code (e.g. a background job task) that needs to
    open its own `Session` outside the request/response cycle."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    return factory


def get_session(request: Request) -> Iterator[Session]:
    """Yield one `Session` bound to this app's engine, closed at the end of the request."""
    session_factory = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def get_settings() -> Settings:
    """Return the current `Settings`, read fresh from `config.toml` (A4: it is the source of
    truth, and `PUT /api/settings` rewrites it — a request-scoped read must never serve a stale
    in-memory copy from app start). `load_settings()` itself falls back to defaults when
    `config.toml` does not exist yet."""
    return load_settings()


def get_job_runner(request: Request) -> JobRunner:
    """Return the app's single background job runner."""
    job_runner: JobRunner = request.app.state.job_runner
    return job_runner


#: `Annotated[..., Depends(...)]` aliases (FastAPI's recommended style): the dependency call
#: lives in the type annotation, not in the argument default, so it never trips `ruff`'s B008
#: ("no function call as an argument default") the way `session: Session = Depends(get_session)`
#: would.
SessionDep = Annotated[Session, Depends(get_session)]
SessionFactoryDep = Annotated["sessionmaker[Session]", Depends(get_session_factory)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
JobRunnerDep = Annotated[JobRunner, Depends(get_job_runner)]
