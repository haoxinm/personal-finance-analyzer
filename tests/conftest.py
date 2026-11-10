"""Shared pytest fixtures (§0.3, P0-1).

- `home`: a temporary `PFA_HOME`, isolated per test.
- Process-wide isolation from the real HOME, data directory and keychain (import time + autouse).
- `engine` / `session`: a migrated temporary SQLite database with the taxonomy synced.
- An autouse guard that fails any test opening a network socket to anything other than
  `127.0.0.1`/`::1`/`localhost` (§6.5: zero network calls in CI).
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import tempfile
from collections.abc import Generator, Iterator
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from personal_finance_analyzer.backup import crypto
from personal_finance_analyzer.classify.taxonomy import sync_taxonomy
from personal_finance_analyzer.core.paths import PFA_HOME_ENV
from personal_finance_analyzer.db.migrate import upgrade_head
from personal_finance_analyzer.db.session import make_engine_for_path, make_session_factory

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


def _isolate_process_from_the_real_user_profile() -> None:
    """Point `HOME`, `PFA_HOME` and the keyring at scratch values for the whole test
    process, at conftest import.

    The per-test autouse fixtures below do the same per test, but module- and session-scoped
    fixtures (and collection) run before them: `tests/ingest/test_layout_c.py` generates fixtures
    at module setup, which reads the tax parameters from the data directory. With no override set
    that resolves the platform default under the *real* home, so a test run could read, or (for
    any code that creates or moves things there) change, a developer's own data directory or
    pop a dialog from the real OS keychain.
    """
    scratch = Path(tempfile.mkdtemp(prefix="pfa-tests-"))
    atexit.register(shutil.rmtree, scratch, ignore_errors=True)
    for name in [n for n in os.environ if n.startswith("PFA_")]:
        del os.environ[name]
    user_home = str(scratch / "user_home")
    os.environ["PFA_HOME"] = str(scratch / "data_home")
    os.environ["HOME"] = user_home
    os.environ["USERPROFILE"] = user_home
    os.environ["LOCALAPPDATA"] = str(scratch / "user_home" / "AppData" / "Local")
    os.environ["APPDATA"] = str(scratch / "user_home" / "AppData" / "Roaming")
    os.environ.pop("XDG_DATA_HOME", None)
    os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"


_isolate_process_from_the_real_user_profile()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary data directory, set as `PFA_HOME` for the duration of the test."""
    data_home = tmp_path / "pfa_home"
    data_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(PFA_HOME_ENV, str(data_home))
    return data_home


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets its own data directory, so code that reads optional local files (the
    tax-parameter cache and override) never sees a real user's data. `home` replaces it."""
    monkeypatch.setenv(PFA_HOME_ENV, str(tmp_path / "auto_home"))
    # Belt and braces: even a test that clears the override must resolve the platform default
    # under a scratch home, never the developer's own.
    fake_user_home = str(tmp_path / "fake_user_home")
    monkeypatch.setenv("HOME", fake_user_home)
    monkeypatch.setenv("USERPROFILE", fake_user_home)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "fake_user_home" / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "fake_user_home" / "AppData" / "Roaming"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


class _MemoryKeyring(keyring.backend.KeyringBackend):
    """An in-process keychain: nothing a test stores ever reaches the developer's real one."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self._store:
            raise keyring.errors.PasswordDeleteError("not found")
        del self._store[(service, username)]


@pytest.fixture(autouse=True)
def _in_memory_keyring() -> Iterator[None]:
    """Every test runs against an in-memory keyring backend, so a test that reaches the keychain
    without its own fake (for instance `doctor`, which round-trips a probe entry) can never touch,
    or pop a dialog from, the real OS keychain. Tests with their own fakes patch the `keyring`
    functions on top of this."""
    previous = keyring.get_keyring()
    keyring.set_keyring(_MemoryKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)


@pytest.fixture
def engine(home: Path) -> Iterator[Engine]:
    """A migrated temporary SQLite database (taxonomy not yet synced)."""
    db_file = home / "finance.db"
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
