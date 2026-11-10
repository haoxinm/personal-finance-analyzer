"""Keyring calls are serialised behind `KEYRING_LOCK` (macOS Security.framework via ctypes)."""

from __future__ import annotations

import threading

import pytest

from personal_finance_analyzer import config as config_module
from personal_finance_analyzer.core.native_locks import KEYRING_LOCK


def test_keyring_calls_hold_lock_and_never_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"active": 0, "max_active": 0, "calls": 0, "unlocked_calls": 0}
    guard = threading.Lock()
    store: dict[tuple[str, str], str] = {}

    def enter() -> None:
        with guard:
            state["calls"] += 1
            if not KEYRING_LOCK.locked():
                state["unlocked_calls"] += 1
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])

    def leave() -> None:
        with guard:
            state["active"] -= 1

    def get_password(service: str, account: str) -> str | None:
        enter()
        try:
            return store.get((service, account))
        finally:
            leave()

    def set_password(service: str, account: str, key: str) -> None:
        enter()
        try:
            store[(service, account)] = key
        finally:
            leave()

    def delete_password(service: str, account: str) -> None:
        enter()
        try:
            store.pop((service, account), None)
        finally:
            leave()

    monkeypatch.setattr(config_module.keyring, "get_password", get_password)
    monkeypatch.setattr(config_module.keyring, "set_password", set_password)
    monkeypatch.setattr(config_module.keyring, "delete_password", delete_password)

    def hammer(n: int) -> None:
        for _ in range(50):
            config_module.set_api_key(f"acct-{n}", "secret")
            config_module.get_api_key(f"acct-{n}")
            config_module.delete_api_key(f"acct-{n}")

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads)
    assert state["calls"] == 8 * 50 * 3
    assert state["unlocked_calls"] == 0
    assert state["max_active"] == 1
    assert not KEYRING_LOCK.locked()
