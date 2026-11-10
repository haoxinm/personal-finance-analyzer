"""Shared pytest fixtures (§0.3, P0-1).

- `home`: a temporary `SPEND_ANALYZER_HOME`, isolated per test.
- `engine` / `session`: a migrated temporary SQLite database with the taxonomy synced.
- An autouse guard that fails any test opening a network socket to anything other than
  `127.0.0.1`/`::1`/`localhost` (§6.5: zero network calls in CI).
"""

from __future__ import annotations

import socket
from collections.abc import Generator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from spend_analyzer.backup import crypto
from spend_analyzer.classify.taxonomy import sync_taxonomy
from spend_analyzer.core.paths import SPEND_ANALYZER_HOME_ENV
from spend_analyzer.db.migrate import upgrade_head
from spend_analyzer.db.session import make_engine_for_path, make_session_factory

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary data directory, set as `SPEND_ANALYZER_HOME` for the duration of the test."""
    data_home = tmp_path / "spend_analyzer_home"
    data_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(SPEND_ANALYZER_HOME_ENV, str(data_home))
    return data_home


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets its own data directory, so code that reads optional local files (the
    tax-parameter cache and override) never sees a real user's data. `home` replaces it."""
    monkeypatch.setenv(SPEND_ANALYZER_HOME_ENV, str(tmp_path / "auto_home"))


@pytest.fixture
def engine(home: Path) -> Iterator[Engine]:
    """A migrated temporary SQLite database (taxonomy not yet synced)."""
    db_file = home / "spend.db"
    upgrade_head(f"sqlite:///{db_file}")
    eng = make_engine_for_path(db_file)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Generator[Session, None, None]:
    """A `Session` on the migrated temp database, with the taxonomy synced."""
    factory = make_session_factory(engine)
    with factory() as db_session:
        sync_taxonomy(db_session)
        db_session.commit()
        yield db_session


@pytest.fixture(autouse=True)
def _cheap_backup_kdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Encrypted backups derive their key with scrypt (128 MiB, a few hundred ms at the shipped
    cost); lower it so the suite stays fast. The cost is read from each file's header on decrypt,
    so this does not weaken what the tests prove."""
    monkeypatch.setattr(crypto, "SCRYPT_LOG2_N", 10)


class _NetworkGuardError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _no_network_except_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a socket connection to a host other than localhost."""
    real_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: object, *args: object) -> object:
        host = _extract_host(address)
        if host is not None and host not in _LOCAL_HOSTS:
            raise _NetworkGuardError(
                f"test attempted a network connection to {host!r}; only 127.0.0.1/localhost "
                "is allowed under test (§6.5)"
            )
        return real_connect(self, address, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def _extract_host(address: object) -> str | None:
    if isinstance(address, tuple) and address and isinstance(address[0], str):
        return address[0]
    return None
