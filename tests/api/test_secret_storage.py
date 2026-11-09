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


# --- read-only status, and environment overrides ---------------------------------------------


class _Recording(_Backend):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []

    def set_password(self, service: str, username: str, password: str) -> None:
        self.writes.append("set")
        super().set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        self.writes.append("delete")
        super().delete_password(service, username)


def test_status_and_settings_reads_do_not_adopt_a_legacy_key(client: TestClient) -> None:
    fake = _Recording()
    keyring.set_keyring(fake)
    saved = client.get("/api/settings").json()
    saved["llm"]["base_url"] = "https://api.example.com"
    client.put("/api/settings", json=saved)
    account = "llm:api.example.com"
    fake.store[("spend-analyzer", account)] = "old"

    status = client.get("/api/settings/secret-storage").json()
    assert status["key_source"] == "keychain"
    assert status["key_legacy"] is True
    assert client.get("/api/settings").json()["llm"]["has_key"] is True
    assert fake.writes == []
    assert ("spend-analyzer", account) in fake.store


def test_put_settings_does_not_persist_env_overrides(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from personal_finance_analyzer.config import load_file_settings

    monkeypatch.setenv("PFA_SECRETS__ALLOW_FILE_STORE", "true")
    monkeypatch.setenv("PFA_SERVER__PORT", "9123")
    body = client.get("/api/settings").json()
    assert body["server"]["port"] == 9123  # the effective value is what the UI sees
    assert body["env_overrides"] == ["secrets.allow_file_store", "server.port"]
    body["privacy"]["store_pdf_copies"] = not body["privacy"]["store_pdf_copies"]

    response = client.put("/api/settings", json=body)
    assert response.status_code == 200
    assert response.json()["server"]["port"] == 9123
    assert response.json()["env_overrides"] == ["secrets.allow_file_store", "server.port"]

    saved = load_file_settings()
    assert saved.server.port == 8756
    assert saved.secrets.allow_file_store is False
    assert saved.privacy.store_pdf_copies is body["privacy"]["store_pdf_copies"]  # real edit kept

    monkeypatch.delenv("PFA_SECRETS__ALLOW_FILE_STORE")
    monkeypatch.delenv("PFA_SERVER__PORT")
    after = client.get("/api/settings/secret-storage").json()
    assert after["file_store_enabled"] is False
    assert client.get("/api/settings").json()["server"]["port"] == 8756


def test_secret_storage_put_is_refused_while_an_env_var_controls_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, no_keychain: object
) -> None:
    from personal_finance_analyzer.config import load_file_settings

    monkeypatch.setenv("PFA_SECRETS__ALLOW_FILE_STORE", "true")
    status = client.get("/api/settings/secret-storage").json()
    assert status["file_store_enabled"] is True
    assert status["file_store_env_var"] == "PFA_SECRETS__ALLOW_FILE_STORE"

    response = client.put("/api/settings/secret-storage", json={"allow_file_store": False})
    assert response.status_code == 409
    assert "PFA_SECRETS__ALLOW_FILE_STORE" in response.json()["detail"]
    assert load_file_settings().secrets.allow_file_store is False  # and nothing was written


def test_secret_storage_put_does_not_persist_other_env_overrides(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, no_keychain: object
) -> None:
    from personal_finance_analyzer.config import load_file_settings

    monkeypatch.setenv("PFA_SERVER__PORT", "9123")
    on = client.put("/api/settings/secret-storage", json={"allow_file_store": True})
    assert on.status_code == 200
    saved = load_file_settings()
    assert saved.secrets.allow_file_store is True
    assert saved.server.port == 8756
