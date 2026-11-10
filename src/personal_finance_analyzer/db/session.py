"""Engine factory, session factory, and the transactional `session_scope` context manager
(§3.2, P0-3).

Uses a **synchronous** SQLAlchemy engine with ``check_same_thread=False``; every FastAPI route
that touches the DB is a plain ``def`` (not ``async def``) so Starlette runs it in its threadpool,
per §3.2 — mixing an async engine with sync sessions is not supported here.

Every new connection gets the four PRAGMAs required by §3.2, applied via a SQLAlchemy
``connect`` event listener:

- ``journal_mode=WAL``
- ``foreign_keys=ON`` (A9 — SQLite defaults this OFF; without it, ``ON DELETE CASCADE`` is silently
  inert)
- ``busy_timeout=5000``
- ``synchronous=NORMAL`` (safe under WAL for a local, single-writer app)
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection as SQLite3Connection

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from personal_finance_analyzer.core.paths import db_path, ensure_home


def _apply_pragmas(dbapi_connection: object, _connection_record: object) -> None:
    if not isinstance(dbapi_connection, SQLite3Connection):  # pragma: no cover - defensive
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def make_engine(db_url: str | None = None) -> Engine:
    """Create a SQLAlchemy engine for the SQLite database at ``db_url``, or the default data
    directory's ``finance.db`` when omitted. Applies the required PRAGMAs on every new connection."""
    if db_url is None:
        ensure_home()
        url = f"sqlite:///{db_path()}"
    else:
        url = db_url
    engine = create_engine(url, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def make_engine_for_path(path: Path) -> Engine:
    """Create an engine for an explicit SQLite file path (used by tests and `migrate`)."""
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a `Session`; commit on success, roll back and re-raise on any exception."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_default_engine() -> Engine:
    """Return an engine bound to the default data directory's database, creating the directory
    if needed."""
    return make_engine()


def iter_session(session_factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    """FastAPI-style dependency: yield one `Session` per request, closing it afterward."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
