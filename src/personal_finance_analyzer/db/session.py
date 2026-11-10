"""Engine factory, session factory, and the transactional `session_scope` context manager
(§3.2, P0-3).

Uses a **synchronous** SQLAlchemy engine with ``check_same_thread=False``; every FastAPI route
that touches the DB is a plain ``def`` (not ``async def``) so Starlette runs it in its threadpool,
per §3.2 — mixing an async engine with sync sessions is not supported here.

Every new connection gets the four PRAGMAs required by §3.2, applied via a SQLAlchemy
``connect`` event listener:

- ``journal_mode=WAL`` (a rollback journal instead when ``PFA_DB_JOURNAL_MODE`` says so, or
  automatically when the data directory is on a network filesystem, where WAL is unsafe; see
  `choose_journal_mode`)
- ``foreign_keys=ON`` (A9 — SQLite defaults this OFF; without it, ``ON DELETE CASCADE`` is silently
  inert)
- ``busy_timeout=5000``
- ``synchronous=NORMAL`` (safe under WAL for a local, single-writer app; ``FULL`` for any other
  journal mode, where NORMAL can lose the last transaction on a power cut)
"""

from __future__ import annotations

import os
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection as SQLite3Connection

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from personal_finance_analyzer.core.fs_check import filesystem_type, is_network_filesystem
from personal_finance_analyzer.core.paths import db_path, ensure_home

#: Environment variable that picks SQLite's journal mode (see `choose_journal_mode`).
JOURNAL_MODE_ENV = "PFA_DB_JOURNAL_MODE"

#: The modes it accepts. ``wal`` is the default; the others are rollback journals, which work
#: where WAL does not (network filesystems).
JOURNAL_MODES = ("wal", "delete", "truncate", "persist")


@dataclass(frozen=True)
class JournalChoice:
    """The journal mode to use and why (``default``, ``env`` or ``network-fs``)."""

    mode: str
    reason: str
    #: The filesystem type that forced the fallback (``network-fs`` only), for the warning.
    fstype: str | None = None


def choose_journal_mode(db_dir: Path | None = None) -> JournalChoice:
    """Resolve the journal mode: ``PFA_DB_JOURNAL_MODE`` when set, else ``wal``, except that a
    database directory on a network filesystem (NFS, SMB/CIFS, ...; Linux only, detected from the
    mount table) gets ``delete``, because WAL needs shared memory and locks those do not provide.

    Raises:
        ValueError: ``PFA_DB_JOURNAL_MODE`` is set to something other than `JOURNAL_MODES`.
    """
    raw = os.environ.get(JOURNAL_MODE_ENV, "").strip().lower()
    if raw:
        if raw not in JOURNAL_MODES:
            raise ValueError(
                f"{JOURNAL_MODE_ENV}={raw!r} is not valid; use one of: {', '.join(JOURNAL_MODES)}"
            )
        return JournalChoice(raw, "env")
    if db_dir is not None and is_network_filesystem(db_dir):
        return JournalChoice("delete", "network-fs", filesystem_type(db_dir))
    return JournalChoice("wal", "default")


def storage_warning(db_dir: Path) -> str | None:
    """A one-paragraph warning when ``db_dir`` is on a network filesystem, else None. For
    `pfa serve` to print once at startup.

    Raises:
        ValueError: ``PFA_DB_JOURNAL_MODE`` is invalid (see `choose_journal_mode`).
    """
    choice = choose_journal_mode(db_dir)
    if choice.reason == "network-fs":
        return (
            f"the data directory {db_dir} is on a network filesystem ({choice.fstype}). SQLite "
            "is not safe there under WAL, so the rollback journal (PFA_DB_JOURNAL_MODE=delete) "
            "is used. That is slower, and still risky if two processes ever open the database "
            "at once; keep the data directory (finance.db) on the NAS's local disk and put only "
            "documents or backups on a share."
        )
    if choice.mode == "wal" and choice.reason == "env" and is_network_filesystem(db_dir):
        return (
            f"{JOURNAL_MODE_ENV}=wal on a network filesystem ({filesystem_type(db_dir)}) can "
            "corrupt the database; unset it to let the app pick the safe mode."
        )
    return None


def _pragma_listener(mode: str) -> Callable[[object, object], None]:
    synchronous = "NORMAL" if mode == "wal" else "FULL"

    def apply(dbapi_connection: object, _connection_record: object) -> None:
        if not isinstance(dbapi_connection, SQLite3Connection):  # pragma: no cover - defensive
            return
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA journal_mode={mode.upper()}")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute(f"PRAGMA synchronous={synchronous}")
        cursor.close()

    return apply


def _new_engine(url: str, choice: JournalChoice) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _pragma_listener(choice.mode))
    return engine


def make_engine(db_url: str | None = None) -> Engine:
    """Create a SQLAlchemy engine for the SQLite database at ``db_url``, or the default data
    directory's ``finance.db`` when omitted. Applies the required PRAGMAs on every new connection
    (the journal mode per `choose_journal_mode`, including the network-filesystem check for the
    directory the database file lives in)."""
    if db_url is None:
        ensure_home()
        file = db_path()
        return _new_engine(f"sqlite:///{file}", choose_journal_mode(file.parent))
    database = make_url(db_url).database
    directory = Path(database).parent if database and database != ":memory:" else None
    return _new_engine(db_url, choose_journal_mode(directory))


def make_engine_for_path(path: Path) -> Engine:
    """Create an engine for an explicit SQLite file path (used by tests, `migrate` and restore),
    with the same journal-mode choice (network-filesystem check included) as `make_engine`."""
    return _new_engine(f"sqlite:///{path}", choose_journal_mode(path.parent))


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
