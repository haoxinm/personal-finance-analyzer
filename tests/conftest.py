"""Shared pytest fixtures (§0.3, P0-1).

- `home`: a temporary `PFA_HOME`, isolated per test.
- Process-wide isolation from the real HOME, data directory and keychain (import time + autouse).
- `engine` / `session`: a migrated temporary SQLite database with the taxonomy synced.
- An autouse guard that fails any test opening a network socket to anything other than
  `127.0.0.1`/`::1`/`localhost` (§6.5: zero network calls in CI).
- A session-level guard against the real world: at start it checks that `HOME` and `PFA_HOME` point
  into a scratch directory and that no real keyring backend is active; at the end it re-stats the
  developer's real data directories (read-only) and fails the run if anything in them changed.
- `@pytest.mark.linux_keyring` tests (the only ones allowed to use a real keyring backend, a
  Secret Service on a throwaway D-Bus session) are skipped unless `PFA_TEST_LINUX_KEYRING=1`.
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import sys
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
from personal_finance_analyzer.core.paths import PFA_HOME_ENV, default_home, legacy_default_home
from personal_finance_analyzer.db.migrate import upgrade_head
from personal_finance_analyzer.db.session import make_engine_for_path, make_session_factory

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", ""}

#: Set (only in CI or a container, inside `dbus-run-session` with a throwaway gnome-keyring) to run
#: the `linux_keyring` tests against a real Secret Service.
LINUX_KEYRING_ENV = "PFA_TEST_LINUX_KEYRING"


def _real_user_home() -> Path:
    """The account's real home directory, read from the OS rather than from `$HOME`."""
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):  # Windows
        return Path.home()


def _real_data_dirs() -> tuple[Path, ...]:
    """The data directories of the machine's real user (current and pre-rename), resolved *before*
    the process is pointed at a scratch home. Resolving is pure (no filesystem access). Besides
    what `$HOME` says, the account's home from the OS is covered too, so a developer who runs the
    suite with a stray `HOME` is still watched."""
    os_home = _REAL_HOME
    dirs = [default_home(), legacy_default_home()]
    if sys.platform == "darwin":
        support = os_home / "Library" / "Application Support"
        dirs += [support / "PersonalFinanceAnalyzer", support / "SpendAnalyzer"]
    elif sys.platform.startswith("linux"):
        share = os_home / ".local" / "share"
        dirs += [share / "personal-finance-analyzer", share / "spend-analyzer"]
    return tuple(dict.fromkeys(dirs))


_REAL_HOME = _real_user_home()
_REAL_DATA_DIRS = _real_data_dirs()


def _snapshot(root: Path) -> dict[str, tuple[int, int]] | None:
    """Every path under ``root`` as ``{relative path: (size, mtime_ns)}``; None if it is absent.
    Read-only (``lstat``), so taking it can never change what it measures."""
    if not root.exists():
        return None
    result: dict[str, tuple[int, int]] = {}
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            st = current.lstat()
        except OSError:
            continue
        result[str(current.relative_to(root))] = (st.st_size, st.st_mtime_ns)
        if current.is_dir() and not current.is_symlink():
            try:
                stack.extend(current.iterdir())
            except OSError:
                continue
    return result


_REAL_DIR_SNAPSHOTS: dict[Path, dict[str, tuple[int, int]] | None] = {
    path: _snapshot(path) for path in _REAL_DATA_DIRS
}


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
    for name in [n for n in os.environ if n.startswith(("PFA_", "SPEND_ANALYZER_"))]:
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


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _assert_isolated() -> None:
    """Fail loudly if the process could reach the real home data directory or a real keychain."""
    scratch_roots = (Path(tempfile.gettempdir()), Path(tempfile.gettempdir()).resolve())
    for name in ("HOME", PFA_HOME_ENV):
        value = os.environ.get(name)
        assert value, f"{name} is not set: the tests could resolve the real data directory"
        path = Path(value)
        assert any(_under(path, root) for root in scratch_roots), (
            f"{name}={value} is outside the temp directory"
        )
    for real in _REAL_DATA_DIRS:
        assert not _under(Path(os.environ[PFA_HOME_ENV]), real), "PFA_HOME is a real data dir"


@pytest.fixture(scope="session", autouse=True)
def _session_isolation_guard() -> None:
    """The same check as `pytest_sessionstart`, for runs where this conftest was not an initial
    one: nothing in the session may start with a path or keyring that reaches the real world."""
    _assert_isolated()
    assert not _is_real_keyring(keyring.get_keyring()) or os.environ.get(LINUX_KEYRING_ENV) == "1"


def _is_real_keyring(backend: object) -> bool:
    module = type(backend).__module__
    return module.startswith("keyring.backends.") and not module.endswith((".fail", ".null"))


def pytest_sessionstart(session: pytest.Session) -> None:
    _assert_isolated()
    # Collection and module-level fixtures run before any per-test fixture: make sure the keyring
    # is already the in-memory fake (unless this run is the opt-in real-Secret-Service one).
    if os.environ.get(LINUX_KEYRING_ENV) != "1":
        keyring.set_keyring(_MemoryKeyring())
        assert isinstance(keyring.get_keyring(), _MemoryKeyring)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """The run must not have changed, created or moved anything in the real data directories
    (read-only comparison with the snapshot taken at import)."""
    changed = [
        str(path) for path, before in _REAL_DIR_SNAPSHOTS.items() if _snapshot(path) != before
    ]
    if changed:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        message = (
            "TEST ISOLATION VIOLATION: the test run changed the real data directory: "
            + ", ".join(changed)
        )
        if reporter is not None:
            reporter.write_line(message, red=True)
        else:  # pragma: no cover
            print(message, file=sys.stderr)
        session.exitstatus = 1


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    enabled = os.environ.get(LINUX_KEYRING_ENV) == "1" and sys.platform == "linux"
    if enabled:
        return
    skip = pytest.mark.skip(
        reason=f"needs a Linux Secret Service: set {LINUX_KEYRING_ENV}=1 under dbus-run-session"
    )
    for item in items:
        if "linux_keyring" in item.keywords:
            item.add_marker(skip)


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


@pytest.fixture(autouse=True)
def _no_legacy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The product's pre-rename `SPEND_ANALYZER_*` variables are still honored as a fallback, so a
    developer's shell that still exports one must not leak into a test. Tests of the fallback set
    their own."""
    for name in [n for n in os.environ if n.startswith("SPEND_ANALYZER_")]:
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _no_terminal_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI sets the `GITHUB_ACTIONS` / `FORCE_COLOR` variables, which make Typer's Rich help force
    ANSI escapes (Typer reads them once, at import, into `typer.rich_utils.FORCE_TERMINAL`).
    Output assertions must not depend on the runner's colour settings, so turn colour off for
    every test."""
    for name in ("FORCE_COLOR", "PY_COLORS", "CLICOLOR_FORCE", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    from typer import rich_utils

    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", False)


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
def _in_memory_keyring(request: pytest.FixtureRequest) -> Iterator[None]:
    """Every test runs against an in-memory keyring backend, so a test that reaches the keychain
    without its own fake (for instance `pfa doctor`, which round-trips a probe entry) can never
    touch, or pop a dialog from, the real OS keychain. Tests with their own fakes patch the
    `keyring` functions on top of this."""
    if request.node.get_closest_marker("linux_keyring") is not None:
        # The one opt-in exception: these tests bring their own real Secret Service backend (and
        # are skipped unless the environment provides a throwaway one).
        yield
        return
    previous = keyring.get_keyring()
    fake = _MemoryKeyring()
    keyring.set_keyring(fake)
    assert keyring.get_keyring() is fake  # nothing after this line can reach a real keychain
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
