"""Secret storage: backend detection, error messages, the opt-in file store, key resolution order
and the `pfa doctor` / settings-API surfaces. Every test runs on fake or in-memory backends (the
suite-wide guard in `tests/conftest.py` makes a real keychain unreachable)."""

from __future__ import annotations

import os
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import keyring
import keyring.backend
import keyring.backends.chainer
import keyring.backends.fail
import keyring.backends.null
import keyring.errors
import pytest
from typer.testing import CliRunner

from personal_finance_analyzer import config as config_module
from personal_finance_analyzer import secret_store
from personal_finance_analyzer.cli import app
from personal_finance_analyzer.config import (
    LLMConfig,
    delete_api_key,
    get_api_key,
    get_llm_api_key,
    keychain_write_test,
    secret_storage_status,
    set_api_key,
)
from personal_finance_analyzer.secret_store import (
    SecretStorageError,
    explain_keyring_error,
    file_store_path,
    inspect_keychain,
    platform_guidance,
)

runner = CliRunner()
_CFG = LLMConfig(base_url="https://api.example.com", model="m")
_ACCOUNT = _CFG.key_account
_SERVICE = "personal-finance-analyzer"


class _Backend(keyring.backend.KeyringBackend):
    """A scriptable in-memory backend that records every call it receives."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}
        self.calls: list[str] = []
        self.fail_with: Callable[[str], Exception | None] = lambda op: None

    def _maybe_fail(self, op: str) -> None:
        self.calls.append(op)
        error = self.fail_with(op)
        if error is not None:
            raise error

    def get_password(self, service: str, username: str) -> str | None:
        self._maybe_fail("get")
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._maybe_fail("set")
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._maybe_fail("delete")
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.store[(service, username)]


def _named(module: str, name: str, base: type = _Backend) -> keyring.backend.KeyringBackend:
    """An instance of a class that claims to live in ``module`` (for backend classification)."""
    cls = type(name, (base,), {"__module__": module})
    instance: keyring.backend.KeyringBackend = cls()
    return instance


@pytest.fixture
def backend() -> _Backend:
    fake = _Backend()
    keyring.set_keyring(fake)  # the suite's autouse fixture restores the previous one
    return fake


@pytest.fixture
def no_keychain() -> keyring.backend.KeyringBackend:
    fail = keyring.backends.fail.Keyring()
    keyring.set_keyring(fail)
    return fail


@pytest.fixture
def file_store_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_SECRETS__ALLOW_FILE_STORE", "true")


@pytest.fixture(autouse=True)
def _clean_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PFA_LLM_API_KEY", "SPEND_ANALYZER_LLM_API_KEY", "PFA_SECRETS__ALLOW_FILE_STORE"):
        monkeypatch.delenv(name, raising=False)


# --- backend detection -----------------------------------------------------------------------


def test_the_suite_runs_on_a_fake_keychain() -> None:
    status = inspect_keychain()
    assert status.usable
    assert "_MemoryKeyring" in status.backend


@pytest.mark.parametrize(
    ("module", "name", "expected"),
    [
        ("keyring.backends.macOS", "Keyring", "macOS Keychain"),
        ("keyring.backends.Windows", "WinVaultKeyring", "Windows Credential Manager"),
        ("keyring.backends.SecretService", "Keyring", "Secret Service (GNOME Keyring / KWallet)"),
        ("keyring.backends.kwallet", "DBusKeyring", "KWallet"),
    ],
)
def test_known_secure_backends_are_named(module: str, name: str, expected: str) -> None:
    keyring.set_keyring(_named(module, name))
    status = inspect_keychain()
    assert status.usable
    assert status.name == expected


def test_fail_backend_is_not_a_keychain(no_keychain: object) -> None:
    status = inspect_keychain()
    assert not status.usable
    assert status.backend == "keyring.backends.fail.Keyring"
    assert "no usable keyring backend" in status.detail


def test_null_backend_is_not_a_keychain() -> None:
    keyring.set_keyring(keyring.backends.null.Keyring())
    status = inspect_keychain()
    assert not status.usable  # it would silently discard every write
    assert "null" in status.detail


def test_plaintext_alt_backend_is_refused() -> None:
    keyring.set_keyring(_named("keyrings.alt.file", "PlaintextKeyring"))
    status = inspect_keychain()
    assert not status.usable
    assert "plaintext" in status.detail


def test_chainer_is_unwrapped_to_its_first_backend() -> None:
    inner = _named("keyring.backends.SecretService", "Keyring")

    class Chain(keyring.backends.chainer.ChainerBackend):
        backends = [inner]  # type: ignore[assignment]  # noqa: RUF012

    keyring.set_keyring(Chain())
    assert inspect_keychain().name == "Secret Service (GNOME Keyring / KWallet)"

    class EmptyChain(keyring.backends.chainer.ChainerBackend):
        backends = []  # type: ignore[assignment]  # noqa: RUF012

    keyring.set_keyring(EmptyChain())
    assert not inspect_keychain().usable


def test_inspecting_never_reads_or_writes_a_secret(backend: _Backend) -> None:
    inspect_keychain()
    secret_storage_status(None)
    assert backend.calls == []


# --- error messages --------------------------------------------------------------------------


def test_explain_no_keyring_error_has_platform_guidance() -> None:
    message = explain_keyring_error(keyring.errors.NoKeyringError("no backend"), platform="linux")
    assert "No system keychain" in message
    assert "gnome-keyring" in message
    assert "dbus-run-session" in message


def test_explain_locked_keychain() -> None:
    message = explain_keyring_error(keyring.errors.KeyringLocked("nope"), platform="darwin")
    assert "locked" in message
    assert "unlock-keychain" in message


def test_explain_macos_keychain_not_found() -> None:
    exc = keyring.errors.PasswordSetError(
        "Can't store password on keychain: (-25307, 'A keychain cannot be found to store 'x'')"
    )
    message = explain_keyring_error(exc, platform="darwin")
    assert "cannot be found" in message
    assert "Keychain Access" in message


def test_explain_init_error() -> None:
    assert "could not be initialised" in explain_keyring_error(keyring.errors.InitError("boom"))


def test_guidance_covers_every_platform() -> None:
    assert "Keychain Access" in platform_guidance("darwin")
    assert "Credential Manager" in platform_guidance("win32")
    for tool in ("apt install gnome-keyring", "dnf install gnome-keyring", "pacman -S"):
        assert tool in platform_guidance("linux")


# --- reading keys ----------------------------------------------------------------------------


def test_resolution_order_env_then_keychain_then_file(
    backend: _Backend, file_store_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_store.write_file_key(_ACCOUNT, "from-file")
    assert get_llm_api_key(_CFG) == "from-file"  # keychain empty, file store opted in
    backend.store[(_SERVICE, _ACCOUNT)] = "from-keychain"
    assert get_llm_api_key(_CFG) == "from-keychain"
    monkeypatch.setenv("PFA_LLM_API_KEY", "from-env")
    assert get_llm_api_key(_CFG) == "from-env"


def test_file_store_is_ignored_unless_opted_in(backend: _Backend) -> None:
    secret_store.write_file_key(_ACCOUNT, "from-file")
    assert get_llm_api_key(_CFG) is None


def test_unavailable_keychain_reads_as_no_key(no_keychain: object) -> None:
    assert get_api_key(_ACCOUNT) is None
    assert get_llm_api_key(_CFG) is None


def test_unavailable_keychain_falls_through_to_the_file_store(
    no_keychain: object, file_store_on: None
) -> None:
    secret_store.write_file_key(_ACCOUNT, "from-file")
    assert get_llm_api_key(_CFG) == "from-file"


@pytest.mark.parametrize(
    "error",
    [
        keyring.errors.KeyringLocked("locked"),
        keyring.errors.InitError("init"),
        keyring.errors.NoKeyringError("none"),
        keyring.errors.KeyringError("-25307 A keychain cannot be found"),
    ],
)
def test_keychain_errors_on_read_never_crash(backend: _Backend, error: Exception) -> None:
    backend.fail_with = lambda op: error
    assert get_api_key(_ACCOUNT) is None
    assert get_llm_api_key(_CFG) is None


def test_a_failing_keychain_read_falls_back_to_the_opted_in_file(
    backend: _Backend, file_store_on: None
) -> None:
    secret_store.write_file_key(_ACCOUNT, "from-file")
    backend.fail_with = lambda op: keyring.errors.KeyringLocked("locked")
    assert get_llm_api_key(_CFG) == "from-file"


# --- legacy service migration ----------------------------------------------------------------


def test_legacy_lookup_is_read_only_when_there_is_no_old_entry(backend: _Backend) -> None:
    assert get_api_key(_ACCOUNT) is None
    assert backend.calls == ["get", "get"]  # the new and the old service; no write, no delete


def test_legacy_key_is_adopted_only_when_present(backend: _Backend) -> None:
    backend.store[("spend-analyzer", "anthropic")] = "old"
    assert get_api_key("anthropic") == "old"
    assert backend.store[(_SERVICE, "anthropic")] == "old"
    assert ("spend-analyzer", "anthropic") not in backend.store


def test_legacy_read_failure_is_tolerated(backend: _Backend) -> None:
    backend.fail_with = lambda op: (
        keyring.errors.KeyringError("old service unreadable") if op == "get" else None
    )
    assert get_api_key("anthropic") is None


def test_legacy_adoption_does_not_delete_the_old_entry_when_the_copy_fails(
    backend: _Backend,
) -> None:
    backend.store[("spend-analyzer", "anthropic")] = "old"
    backend.fail_with = lambda op: keyring.errors.KeyringLocked("locked") if op == "set" else None
    assert get_api_key("anthropic") == "old"
    assert ("spend-analyzer", "anthropic") in backend.store


# --- saving keys -----------------------------------------------------------------------------


def test_set_stores_in_the_keychain_and_reports_it(backend: _Backend) -> None:
    assert set_api_key(_ACCOUNT, "k1") == "keychain"
    assert backend.store[(_SERVICE, _ACCOUNT)] == "k1"
    assert not file_store_path().exists()


def test_set_with_no_keychain_and_no_opt_in_fails_with_guidance(no_keychain: object) -> None:
    with pytest.raises(SecretStorageError) as caught:
        set_api_key(_ACCOUNT, "secret-value")
    message = str(caught.value)
    assert "no usable system keychain" in message
    assert "PFA_LLM_API_KEY" in message or "allow_file_store" in message
    assert "secret-value" not in message
    assert not file_store_path().exists()  # never a silent plaintext fallback


def test_set_with_no_keychain_uses_the_file_store_when_opted_in(
    no_keychain: object, file_store_on: None
) -> None:
    assert set_api_key(_ACCOUNT, "k2") == "file"
    assert file_store_path().is_file()
    assert get_llm_api_key(_CFG) == "k2"


def test_a_working_keychain_beats_the_file_store_for_saving(
    backend: _Backend, file_store_on: None
) -> None:
    assert set_api_key(_ACCOUNT, "k3") == "keychain"
    assert not file_store_path().exists()


def test_saving_to_the_keychain_removes_a_stale_plaintext_copy(
    backend: _Backend, file_store_on: None
) -> None:
    secret_store.write_file_key(_ACCOUNT, "stale")
    set_api_key(_ACCOUNT, "fresh")
    assert not file_store_path().exists()
    assert get_llm_api_key(_CFG) == "fresh"


@pytest.mark.parametrize(
    "error",
    [
        keyring.errors.KeyringLocked("locked"),
        keyring.errors.PasswordSetError("Can't store password on keychain: (-25307)"),
        keyring.errors.InitError("init"),
    ],
)
def test_a_refused_keychain_write_is_an_actionable_error_not_a_crash(
    backend: _Backend, error: Exception
) -> None:
    backend.fail_with = lambda op: error
    with pytest.raises(SecretStorageError) as caught:
        set_api_key(_ACCOUNT, "k4")
    assert "keychain" in str(caught.value)
    assert not file_store_path().exists()


def test_a_refused_keychain_write_uses_the_file_store_only_when_opted_in(
    backend: _Backend, file_store_on: None
) -> None:
    backend.fail_with = lambda op: keyring.errors.KeyringLocked("locked")
    assert set_api_key(_ACCOUNT, "k5") == "file"


def test_delete_removes_from_keychain_and_file(backend: _Backend, file_store_on: None) -> None:
    backend.store[(_SERVICE, _ACCOUNT)] = "a"
    secret_store.write_file_key(_ACCOUNT, "b")
    delete_api_key(_ACCOUNT)
    assert backend.store == {}
    assert not file_store_path().exists()
    delete_api_key(_ACCOUNT)  # idempotent


def test_delete_cleans_the_file_even_without_the_opt_in(no_keychain: object) -> None:
    secret_store.write_file_key(_ACCOUNT, "b")
    delete_api_key(_ACCOUNT)
    assert not file_store_path().exists()


def test_delete_surfaces_a_locked_keychain(backend: _Backend) -> None:
    backend.fail_with = lambda op: keyring.errors.KeyringLocked("locked")
    with pytest.raises(SecretStorageError, match="locked"):
        delete_api_key(_ACCOUNT)


# --- the file store itself -------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_file_store_permissions_are_private() -> None:
    secret_store.write_file_key(_ACCOUNT, "k")
    path = file_store_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_loosened_file_is_tightened_on_read() -> None:
    secret_store.write_file_key(_ACCOUNT, "k")
    path = file_store_path()
    path.chmod(0o644)
    assert secret_store.read_file_key(_ACCOUNT) == "k"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_file_store_round_trip_and_multiple_accounts() -> None:
    secret_store.write_file_key("llm:a.example.com", "ka")
    secret_store.write_file_key("llm:b.example.com", "kb")
    assert secret_store.read_file_key("llm:a.example.com") == "ka"
    assert secret_store.read_file_key("llm:b.example.com") == "kb"
    assert secret_store.read_file_key("llm:c.example.com") is None
    secret_store.delete_file_key("llm:a.example.com")
    assert secret_store.read_file_key("llm:b.example.com") == "kb"
    secret_store.delete_file_key("llm:b.example.com")
    assert not file_store_path().exists()  # an empty store leaves no file behind


def test_file_store_write_is_atomic(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_store.write_file_key(_ACCOUNT, "original")

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", boom)
        with pytest.raises(SecretStorageError):
            secret_store.write_file_key(_ACCOUNT, "replacement")
    assert secret_store.read_file_key(_ACCOUNT) == "original"  # untouched
    assert [p.name for p in file_store_path().parent.iterdir()] == [file_store_path().name]


def test_file_store_survives_concurrent_writers() -> None:
    def write(i: int) -> None:
        secret_store.write_file_key(f"llm:h{i}.example.com", f"k{i}")

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i in range(8):
        assert secret_store.read_file_key(f"llm:h{i}.example.com") == f"k{i}"


def test_corrupt_file_store_is_a_clear_error_not_a_crash(file_store_on: None) -> None:
    path = file_store_path()
    path.parent.mkdir(parents=True)
    path.write_text("this is = not [valid toml")
    with pytest.raises(SecretStorageError, match="could not be read"):
        secret_store.read_file_key(_ACCOUNT)
    assert get_api_key(_ACCOUNT) is None  # a read degrades to "no key"


def test_keys_never_reach_config_toml_or_logs(
    no_keychain: object, file_store_on: None, home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    set_api_key(_ACCOUNT, "sk-very-secret")
    get_llm_api_key(_CFG)
    assert "sk-very-secret" not in caplog.text
    config = home / "config.toml"
    assert not config.exists() or "sk-very-secret" not in config.read_text()
    assert "plaintext" in caplog.text  # the warning was logged


# --- status ----------------------------------------------------------------------------------


def test_status_with_a_working_keychain(backend: _Backend) -> None:
    status = secret_storage_status(_CFG)
    assert status.storage == "keychain"
    assert status.key_source == "none"
    assert status.guidance == ""
    assert status.warning == ""
    backend.store[(_SERVICE, _ACCOUNT)] = "k"
    assert secret_storage_status(_CFG).key_source == "keychain"


def test_status_with_no_keychain_gives_guidance(no_keychain: object) -> None:
    status = secret_storage_status(_CFG)
    assert status.storage == "none"
    assert not status.keychain_usable
    assert status.guidance
    assert status.warning == ""


def test_status_with_the_file_store(no_keychain: object, file_store_on: None) -> None:
    status = secret_storage_status(_CFG)
    assert status.storage == "file"
    assert "plaintext" in status.warning
    set_api_key(_ACCOUNT, "k")
    assert secret_storage_status(_CFG).key_source == "file"


def test_status_with_an_env_var(no_keychain: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEND_ANALYZER_LLM_API_KEY", "legacy")
    status = secret_storage_status(_CFG)
    assert status.key_source == "env"
    assert status.env_var == "SPEND_ANALYZER_LLM_API_KEY"
    monkeypatch.setenv("PFA_LLM_API_KEY", "new")
    assert secret_storage_status(_CFG).env_var == "PFA_LLM_API_KEY"


# --- keychain write test ---------------------------------------------------------------------


def test_write_test_round_trips_and_cleans_up(backend: _Backend) -> None:
    keychain_write_test()
    assert backend.store == {}
    assert backend.calls[:2] == ["set", "get"]


def test_write_test_fails_clearly_without_a_keychain(no_keychain: object) -> None:
    with pytest.raises(SecretStorageError, match="no usable system keychain"):
        keychain_write_test()


def test_write_test_reports_a_refused_write(backend: _Backend) -> None:
    backend.fail_with = lambda op: (
        keyring.errors.PasswordSetError("(-25307, 'A keychain cannot be found')")
        if op == "set"
        else None
    )
    with pytest.raises(SecretStorageError, match="keychain"):
        keychain_write_test()


# --- `pfa doctor` ----------------------------------------------------------------------------


def test_doctor_is_read_only_by_default(home: Path, backend: _Backend) -> None:
    result = runner.invoke(app, ["doctor"])
    assert "PASS  Keychain backend:" in result.output
    assert "SKIP  Keychain write test" in result.output
    assert backend.calls == []  # not even a read: no LLM is configured
    assert backend.store == {}


def test_doctor_write_test_flag_round_trips(home: Path, backend: _Backend) -> None:
    result = runner.invoke(app, ["doctor", "--write-test"])
    assert "PASS  Keychain write test" in result.output
    assert backend.calls[:2] == ["set", "get"]
    assert backend.store == {}  # the probe is deleted again


def test_doctor_write_test_failure_exits_nonzero(home: Path, backend: _Backend) -> None:
    backend.fail_with = lambda op: keyring.errors.KeyringLocked("locked")
    result = runner.invoke(app, ["doctor", "--write-test"])
    assert result.exit_code == 1
    assert "FAIL  Keychain write test failed" in result.output
    assert "locked" in result.output


def test_doctor_reports_no_keychain_with_guidance_but_does_not_fail(
    home: Path, no_keychain: object
) -> None:
    runner.invoke(app, ["migrate"])
    result = runner.invoke(app, ["doctor"])
    assert "No usable OS keychain" in result.output
    assert "PFA_LLM_API_KEY" in result.output
    assert "Key source / save target" in result.output
    assert result.exit_code == 0


def test_doctor_prints_the_plaintext_warning_when_the_file_store_is_on(
    home: Path, no_keychain: object, file_store_on: None
) -> None:
    result = runner.invoke(app, ["doctor"])
    assert "plaintext" in result.output
    assert str(file_store_path()) in result.output.replace("\n", "")


def test_doctor_reads_the_configured_key_without_writing(home: Path, backend: _Backend) -> None:
    runner.invoke(
        app, ["llm", "set", "--base-url", "https://api.example.com", "--model", "m", "--enable"]
    )
    backend.store[(_SERVICE, _ACCOUNT)] = "k"
    backend.calls.clear()
    result = runner.invoke(app, ["doctor"])
    assert "API key for api.example.com readable" in result.output
    assert "set" not in backend.calls and "delete" not in backend.calls


# --- `pfa llm set-key` -----------------------------------------------------------------------


def test_set_key_without_a_keychain_prints_guidance_and_exits_1(
    home: Path, no_keychain: object
) -> None:
    runner.invoke(app, ["llm", "set", "--base-url", "https://api.example.com"])
    result = runner.invoke(app, ["llm", "set-key"], input="sk-secret\n")
    assert result.exit_code == 1
    assert "could not store the key" in result.output
    assert "sk-secret" not in result.output


def test_set_key_into_the_file_store_warns(
    home: Path, no_keychain: object, file_store_on: None
) -> None:
    runner.invoke(app, ["llm", "set", "--base-url", "https://api.example.com"])
    result = runner.invoke(app, ["llm", "set-key"], input="sk-secret\n")
    assert result.exit_code == 0, result.output
    assert "plaintext file store" in result.output
    assert "WARNING" in result.output
    shown = runner.invoke(app, ["llm", "show"]).output
    assert "api key source: plaintext file store" in shown
    assert "sk-secret" not in shown


def test_config_module_exposes_the_secrets_setting() -> None:
    assert config_module.Settings().secrets.allow_file_store is False


# --- read-only legacy lookups (`pfa doctor`, the status screen) ------------------------------


def _legacy_key(backend: _Backend) -> None:
    backend.store[("spend-analyzer", _ACCOUNT)] = "old"


def test_non_adopting_lookup_finds_the_legacy_key_without_writing(backend: _Backend) -> None:
    _legacy_key(backend)
    assert get_llm_api_key(_CFG, adopt=False) == "old"
    assert "set" not in backend.calls and "delete" not in backend.calls
    assert ("spend-analyzer", _ACCOUNT) in backend.store
    assert (_SERVICE, _ACCOUNT) not in backend.store


def test_adopting_lookup_still_moves_the_legacy_key(backend: _Backend) -> None:
    _legacy_key(backend)
    assert get_llm_api_key(_CFG) == "old"
    assert backend.store[(_SERVICE, _ACCOUNT)] == "old"
    assert ("spend-analyzer", _ACCOUNT) not in backend.store


def test_status_reports_a_legacy_key_and_never_writes(backend: _Backend) -> None:
    _legacy_key(backend)
    status = secret_storage_status(_CFG)
    assert status.key_source == "keychain"
    assert status.key_legacy is True
    assert "set" not in backend.calls and "delete" not in backend.calls
    assert ("spend-analyzer", _ACCOUNT) in backend.store


def test_status_without_a_legacy_key_is_not_legacy(backend: _Backend) -> None:
    backend.store[(_SERVICE, _ACCOUNT)] = "new"
    status = secret_storage_status(_CFG)
    assert (status.key_source, status.key_legacy) == ("keychain", False)


def test_doctor_does_not_adopt_a_legacy_key_and_says_so(home: Path, backend: _Backend) -> None:
    runner.invoke(
        app, ["llm", "set", "--base-url", "https://api.example.com", "--model", "m", "--enable"]
    )
    _legacy_key(backend)
    backend.calls.clear()
    result = runner.invoke(app, ["doctor"])
    assert "API key for api.example.com readable" in result.output
    assert "found under the old name" in result.output.replace("\n", " ")
    assert "set" not in backend.calls and "delete" not in backend.calls
    assert ("spend-analyzer", _ACCOUNT) in backend.store
    assert (_SERVICE, _ACCOUNT) not in backend.store


def test_llm_show_does_not_adopt_a_legacy_key(home: Path, backend: _Backend) -> None:
    runner.invoke(app, ["llm", "set", "--base-url", "https://api.example.com", "--model", "m"])
    _legacy_key(backend)
    backend.calls.clear()
    result = runner.invoke(app, ["llm", "show"])
    assert "api key on file: yes" in result.output
    assert "set" not in backend.calls and "delete" not in backend.calls


# --- doctor on a fresh machine ---------------------------------------------------------------


def test_doctor_creates_no_data_directory_or_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: _Backend
) -> None:
    fresh = tmp_path / "fresh" / "PersonalFinanceAnalyzer"
    monkeypatch.setenv("PFA_HOME", str(fresh))
    result = runner.invoke(app, ["doctor"])
    assert "not created yet" in result.output
    assert "No database yet" in result.output
    assert not fresh.exists()
    assert not fresh.parent.exists()


def test_doctor_leaves_an_existing_database_unchanged(home: Path, backend: _Backend) -> None:
    runner.invoke(app, ["migrate"])
    before = (home / "finance.db").read_bytes()
    result = runner.invoke(app, ["doctor"])
    assert "PASS  Database migrated to head" in result.output
    assert (home / "finance.db").read_bytes() == before


# --- environment overrides are never persisted -----------------------------------------------


def test_load_file_settings_ignores_the_environment(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PFA_SECRETS__ALLOW_FILE_STORE", "true")
    assert config_module.load_settings().secrets.allow_file_store is True
    assert config_module.load_file_settings().secrets.allow_file_store is False


def test_env_overridden_fields_and_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEND_ANALYZER_LLM__MODE", "none")
    monkeypatch.setenv("PFA_SERVER__PORT", "9000")
    monkeypatch.setenv("PFA_SERVER__NOPE", "1")
    assert config_module.env_overridden_fields() == {("llm", "enabled"), ("server", "port")}
    assert config_module.env_override_var("server", "port") == "PFA_SERVER__PORT"
    assert config_module.env_override_var("tax", "auto_update_params") is None


def test_llm_set_does_not_write_an_env_override_into_config(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PFA_SECRETS__ALLOW_FILE_STORE", "true")
    monkeypatch.setenv("PFA_SERVER__PORT", "9999")
    result = runner.invoke(app, ["llm", "set", "--model", "m"])
    assert result.exit_code == 0
    saved = config_module.load_file_settings()
    assert saved.secrets.allow_file_store is False
    assert saved.server.port == 8756
    assert saved.llm.model == "m"


# --- removing the plaintext key file for real ------------------------------------------------


def test_shred_file_overwrites_the_data_before_unlinking(tmp_path: Path) -> None:
    from personal_finance_analyzer.core.secure_delete import shred_file

    path = tmp_path / "api_keys.toml"
    path.write_bytes(b'[keys]\n"llm:x" = "sk-very-secret"\n' * 100)
    alias = tmp_path / "alias"
    os.link(path, alias)  # a second name for the same data: what an undelete would still see
    shred_file(path)
    assert not path.exists()
    assert alias.read_bytes() == bytes(alias.stat().st_size)  # zeros, same length
    assert b"sk-very-secret" not in alias.read_bytes()


def test_shred_file_tolerates_missing_and_symlinks(tmp_path: Path) -> None:
    from personal_finance_analyzer.core.secure_delete import shred_file

    shred_file(tmp_path / "missing")
    target = tmp_path / "target"
    target.write_bytes(b"keep me")
    link = tmp_path / "link"
    link.symlink_to(target)
    shred_file(link)
    assert not link.exists()
    assert target.read_bytes() == b"keep me"  # only the link went


def test_shred_tree_removes_files_and_directories(tmp_path: Path) -> None:
    from personal_finance_analyzer.core.secure_delete import shred_tree

    root = tmp_path / "secrets"
    (root / "nested").mkdir(parents=True)
    (root / "a").write_bytes(b"x")
    (root / "nested" / "b").write_bytes(b"y")
    shred_tree(root)
    assert not root.exists()
    shred_tree(root)  # already gone


def test_remove_file_store_takes_every_key_and_stray_temp_files() -> None:
    secret_store.write_file_key("a", "1")
    secret_store.write_file_key("b", "2")
    leftover = file_store_path().parent / ".api_keys-abc.tmp"
    leftover.write_text("half written", encoding="utf-8")
    assert secret_store.remove_file_store() is True
    assert not file_store_path().parent.exists()  # the directory went with the last file
    assert secret_store.remove_file_store() is False  # nothing left to remove


def test_deleting_the_last_key_removes_the_file_and_its_directory() -> None:
    secret_store.write_file_key(_ACCOUNT, "k")
    secret_store.delete_file_key(_ACCOUNT)
    assert not file_store_path().exists()
    assert not file_store_path().parent.exists()


def test_file_store_file_exists_even_when_unreadable() -> None:
    assert secret_store.file_store_file_exists() is False
    file_store_path().parent.mkdir(parents=True)
    file_store_path().write_text("this is [not toml", encoding="utf-8")
    assert secret_store.file_store_file_exists() is True
    assert secret_store.file_store_has_keys() is False  # unreadable counts as no keys...
    assert secret_store.remove_file_store() is True  # ...but it can still be removed


# --- moving the plaintext keys into the keychain ---------------------------------------------


def test_move_file_keys_to_keychain_moves_verifies_and_removes(backend: _Backend) -> None:
    secret_store.write_file_key("llm:a.example", "ka")
    secret_store.write_file_key("llm:b.example", "kb")
    assert config_module.move_file_keys_to_keychain() == (2, 0)
    assert backend.store[(_SERVICE, "llm:a.example")] == "ka"
    assert backend.store[(_SERVICE, "llm:b.example")] == "kb"
    assert not file_store_path().exists()


def test_move_keeps_an_existing_keychain_key_and_drops_the_file_entry(backend: _Backend) -> None:
    backend.store[(_SERVICE, "llm:a.example")] = "newer-keychain"
    secret_store.write_file_key("llm:a.example", "older-file")
    secret_store.write_file_key("llm:b.example", "kb")
    result = config_module.move_file_keys_to_keychain()
    assert (result.moved, result.kept_existing) == (1, 1)
    assert backend.store[(_SERVICE, "llm:a.example")] == "newer-keychain"  # not overwritten
    assert backend.store[(_SERVICE, "llm:b.example")] == "kb"
    assert not file_store_path().exists()


def test_move_removes_a_file_that_holds_no_string_keys(backend: _Backend) -> None:
    file_store_path().parent.mkdir(parents=True)
    file_store_path().write_text("[keys]\nn = 3\n", encoding="utf-8")
    assert config_module.move_file_keys_to_keychain() == (0, 0)
    assert not file_store_path().exists()
    assert backend.store == {}


def test_move_keeps_the_plaintext_when_the_keychain_refuses(backend: _Backend) -> None:
    secret_store.write_file_key(_ACCOUNT, "k")
    backend.fail_with = lambda op: keyring.errors.KeyringLocked("locked") if op == "set" else None
    with pytest.raises(SecretStorageError, match="locked"):
        config_module.move_file_keys_to_keychain()
    assert secret_store.read_file_key(_ACCOUNT) == "k"


def test_move_keeps_the_plaintext_when_the_keychain_does_not_return_the_key(
    backend: _Backend,
) -> None:
    secret_store.write_file_key(_ACCOUNT, "k")
    backend.set_password = lambda service, username, password: None  # type: ignore[method-assign]
    with pytest.raises(SecretStorageError, match="did not return"):
        config_module.move_file_keys_to_keychain()
    assert secret_store.read_file_key(_ACCOUNT) == "k"


def test_move_without_a_keychain_is_an_error(no_keychain: object) -> None:
    secret_store.write_file_key(_ACCOUNT, "k")
    with pytest.raises(SecretStorageError, match="no usable system keychain"):
        config_module.move_file_keys_to_keychain()
    assert secret_store.read_file_key(_ACCOUNT) == "k"


# --- the pre-rename keychain service is not probed unless asked ------------------------------


def _spy_on_services(backend: _Backend) -> list[str]:
    services: list[str] = []
    real_get = backend.get_password

    def spy(service: str, username: str) -> str | None:
        services.append(service)
        return real_get(service, username)

    backend.get_password = spy  # type: ignore[method-assign]
    return services


def test_lookup_without_legacy_probe_never_touches_the_old_service(backend: _Backend) -> None:
    _legacy_key(backend)
    services = _spy_on_services(backend)
    assert get_llm_api_key(_CFG, adopt=False, probe_legacy=False) is None
    assert services == [_SERVICE]


def test_status_without_legacy_probe_never_touches_the_old_service(backend: _Backend) -> None:
    _legacy_key(backend)
    services = _spy_on_services(backend)
    status = secret_storage_status(_CFG, probe_legacy=False)
    assert (status.key_source, status.key_legacy, status.legacy_checked) == ("none", False, False)
    assert services == [_SERVICE]
    assert secret_storage_status(_CFG).legacy_checked is True  # the CLI default still checks
