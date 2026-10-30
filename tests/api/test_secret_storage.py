"""`/api/settings/secret-storage`, and what the key routes do when there is no keychain."""

from __future__ import annotations

import keyring
import keyring.backend
import keyring.backends.fail
import pytest
from fastapi.testclient import TestClient

from personal_finance_analyzer.secret_store import file_store_path


class _Backend(keyring.backend.KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


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


@pytest.fixture(autouse=True)
def _clean_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PFA_LLM_API_KEY", "SPEND_ANALYZER_LLM_API_KEY", "PFA_SECRETS__ALLOW_FILE_STORE"):
        monkeypatch.delenv(name, raising=False)


def test_api_reports_the_backend(client: TestClient, backend: _Backend) -> None:
    body = client.get("/api/settings/secret-storage").json()
    assert body["storage"] == "keychain"
    assert body["keychain_usable"] is True
    assert body["file_store_enabled"] is False
    assert body["key_source"] == "none"


def test_api_reports_no_backend_with_guidance(client: TestClient, no_keychain: object) -> None:
    body = client.get("/api/settings/secret-storage").json()
    assert body["storage"] == "none"
    assert body["keychain_usable"] is False
    assert body["guidance"]
    assert body["warning"] == ""


def test_api_put_api_key_without_a_backend_is_503_with_a_message(
    client: TestClient, no_keychain: object
) -> None:
    saved = client.get("/api/settings").json()
    saved["llm"]["base_url"] = "https://api.example.com"
    client.put("/api/settings", json=saved)
    response = client.put("/api/settings/api-key", json={"api_key": "sk-x"})
    assert response.status_code == 503
    assert "no usable system keychain" in response.json()["detail"]
    assert "sk-x" not in response.text


def test_api_toggle_file_store_then_save_key(client: TestClient, no_keychain: object) -> None:
    saved = client.get("/api/settings").json()
    saved["llm"]["base_url"] = "https://api.example.com"
    client.put("/api/settings", json=saved)

    on = client.put("/api/settings/secret-storage", json={"allow_file_store": True}).json()
    assert on["file_store_enabled"] is True
    assert on["storage"] == "file"
    assert "plaintext" in on["warning"]

    assert client.put("/api/settings/api-key", json={"api_key": "sk-x"}).status_code == 204
    status = client.get("/api/settings/secret-storage").json()
    assert status["key_source"] == "file"
    assert client.get("/api/settings").json()["llm"]["has_key"] is True

    # PUT /settings must not silently reset the opt-in.
    client.put("/api/settings", json=client.get("/api/settings").json())
    assert client.get("/api/settings/secret-storage").json()["file_store_enabled"] is True

    off = client.put("/api/settings/secret-storage", json={"allow_file_store": False}).json()
    assert off["file_store_enabled"] is False
    assert client.delete("/api/settings/api-key").status_code == 204
    assert not file_store_path().exists()
