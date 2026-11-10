"""Secret storage against a real Linux Secret Service (GNOME Keyring over D-Bus).

Skipped everywhere except where `PFA_TEST_LINUX_KEYRING=1` on Linux, which CI sets inside
`dbus-run-session` after starting a throwaway `gnome-keyring-daemon` (see `.github/workflows/ci.yml`
and docs/INSTALL.md). These are the only tests allowed to use a real keyring backend; they use a
unique account per test and remove everything they store.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import keyring
import keyring.errors
import pytest

from personal_finance_analyzer.config import (
    delete_api_key,
    get_api_key,
    keychain_write_test,
    secret_storage_status,
    set_api_key,
)
from personal_finance_analyzer.secret_store import inspect_keychain

pytestmark = pytest.mark.linux_keyring

_SERVICE = "personal-finance-analyzer"


@pytest.fixture(autouse=True)
def _secret_service() -> Iterator[None]:
    """Select the Secret Service backend explicitly (the suite's other fixtures install an
    in-memory one for every other test)."""
    from keyring.backends import SecretService

    previous = keyring.get_keyring()
    try:
        keyring.set_keyring(SecretService.Keyring())
    except keyring.errors.KeyringError as exc:  # pragma: no cover - environment problem
        pytest.fail(f"no Secret Service reachable on D-Bus: {exc}")
    try:
        yield
    finally:
        keyring.set_keyring(previous)


@pytest.fixture
def account() -> Iterator[str]:
    name = f"pfa-test-{uuid.uuid4().hex}"
    try:
        yield name
    finally:
        delete_api_key(name)


def test_the_secret_service_is_detected_and_usable() -> None:
    status = inspect_keychain()
    assert status.usable, status.detail
    assert status.name == "Secret Service (GNOME Keyring / KWallet)"
    assert secret_storage_status(None).storage == "keychain"


def test_set_get_delete_round_trip(account: str) -> None:
    assert get_api_key(account) is None
    assert set_api_key(account, "sk-linux-secret") == "keychain"
    assert get_api_key(account) == "sk-linux-secret"
    # It really is in the keyring, under the app's service name.
    assert keyring.get_password(_SERVICE, account) == "sk-linux-secret"
    delete_api_key(account)
    assert get_api_key(account) is None


def test_overwrite(account: str) -> None:
    set_api_key(account, "one")
    set_api_key(account, "two")
    assert get_api_key(account) == "two"


def test_doctor_write_test_passes() -> None:
    keychain_write_test()
    assert keyring.get_password(_SERVICE, "pfa-doctor-check") is None  # probe cleaned up
