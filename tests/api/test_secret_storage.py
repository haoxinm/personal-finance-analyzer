"""`/api/settings/secret-storage`, and what the key routes do when there is no keychain."""

from __future__ import annotations

import keyring
import keyring.backend
import keyring.backends.fail
import pytest
from fastapi.testclient import TestClient
from httpx import Response

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

    # With no keychain to move it to, turning the file off needs an explicit choice about the key.
    refused = client.put("/api/settings/secret-storage", json={"allow_file_store": False})
    assert refused.status_code == 409
    off = client.put(
        "/api/settings/secret-storage", json={"allow_file_store": False, "file_key_action": "keep"}
    ).json()
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


def test_explicit_legacy_check_reports_the_old_key_without_adopting_it(client: TestClient) -> None:
    fake = _Recording()
    keyring.set_keyring(fake)
    saved = client.get("/api/settings").json()
    saved["llm"]["base_url"] = "https://api.example.com"
    client.put("/api/settings", json=saved)
    account = "llm:api.example.com"
    fake.store[("spend-analyzer", account)] = "old"

    status = client.get("/api/settings/secret-storage?check_legacy=true").json()
    assert status["key_source"] == "keychain"
    assert status["key_legacy"] is True
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


# --- turning the plaintext key file off does not leave the key on disk -----------------------


def _enable_file_store_and_save_a_key(client: TestClient) -> None:
    saved = client.get("/api/settings").json()
    saved["llm"]["base_url"] = "https://api.example.com"
    client.put("/api/settings", json=saved)
    on = client.put("/api/settings/secret-storage", json={"allow_file_store": True})
    assert on.status_code == 200
    assert client.put("/api/settings/api-key", json={"api_key": "sk-plain"}).status_code == 204
    assert file_store_path().is_file()


def _off(client: TestClient, **extra: object) -> Response:
    return client.put("/api/settings/secret-storage", json={"allow_file_store": False, **extra})


def test_turning_off_moves_the_key_into_a_working_keychain(client: TestClient) -> None:
    keyring.set_keyring(keyring.backends.fail.Keyring())
    _enable_file_store_and_save_a_key(client)
    working = _Backend()
    keyring.set_keyring(working)  # a keychain became available since

    response = _off(client)

    assert response.status_code == 200
    body = response.json()
    assert (body["file_key_outcome"], body["moved_keys"]) == ("moved", 1)
    assert body["file_store_enabled"] is False
    assert body["file_store_has_keys"] is False
    assert not file_store_path().exists()
    assert not file_store_path().parent.exists()
    assert working.store[("personal-finance-analyzer", "llm:api.example.com")] == "sk-plain"
    assert client.get("/api/settings/secret-storage").json()["key_source"] == "keychain"


def test_turning_off_keeps_a_newer_keychain_key_and_removes_the_file(client: TestClient) -> None:
    keyring.set_keyring(keyring.backends.fail.Keyring())
    _enable_file_store_and_save_a_key(client)
    working = _Backend()
    working.store[("personal-finance-analyzer", "llm:api.example.com")] = "sk-newer"
    keyring.set_keyring(working)

    body = _off(client).json()

    assert (body["file_key_outcome"], body["moved_keys"], body["kept_keys"]) == ("moved", 0, 1)
    assert working.store[("personal-finance-analyzer", "llm:api.example.com")] == "sk-newer"
    assert not file_store_path().exists()


def test_turning_off_removes_a_file_with_no_string_keys(client: TestClient) -> None:
    keyring.set_keyring(_Backend())
    file_store_path().parent.mkdir(parents=True, exist_ok=True)
    file_store_path().write_text("[keys]\nn = 3\n", encoding="utf-8")

    body = _off(client).json()

    assert (body["file_key_outcome"], body["moved_keys"], body["kept_keys"]) == ("moved", 0, 0)
    assert not file_store_path().exists()


def test_turning_off_without_a_keychain_demands_a_choice_and_changes_nothing(
    client: TestClient, no_keychain: object
) -> None:
    _enable_file_store_and_save_a_key(client)
    response = _off(client)
    assert response.status_code == 409
    assert "delete" in response.json()["detail"] and "keep" in response.json()["detail"]
    assert file_store_path().is_file()
    assert client.get("/api/settings/secret-storage").json()["file_store_enabled"] is True


def test_turning_off_and_deleting_removes_the_file(client: TestClient, no_keychain: object) -> None:
    _enable_file_store_and_save_a_key(client)
    body = _off(client, file_key_action="delete").json()
    assert (body["file_key_outcome"], body["moved_keys"]) == ("deleted", 0)
    assert body["file_store_enabled"] is False
    assert body["file_store_has_keys"] is False
    assert not file_store_path().exists()
    assert client.get("/api/settings").json()["llm"]["has_key"] is False


def test_turning_off_and_keeping_leaves_the_file_unused_and_says_so(
    client: TestClient, no_keychain: object
) -> None:
    _enable_file_store_and_save_a_key(client)
    body = _off(client, file_key_action="keep").json()
    assert body["file_key_outcome"] == "kept"
    assert body["file_store_enabled"] is False
    assert body["file_store_has_keys"] is True  # the UI can state where the key still is
    assert body["file_store_path"] == str(file_store_path())
    assert file_store_path().is_file()
    assert client.get("/api/settings").json()["llm"]["has_key"] is False  # and it is not used
    # The kept file can be dealt with later, without turning anything on again.
    later = _off(client, file_key_action="delete").json()
    assert later["file_key_outcome"] == "deleted"
    assert not file_store_path().exists()


def test_a_failed_move_keeps_the_file_and_the_opt_in(client: TestClient) -> None:
    keyring.set_keyring(keyring.backends.fail.Keyring())
    _enable_file_store_and_save_a_key(client)

    class _Locked(_Backend):
        def set_password(self, service: str, username: str, password: str) -> None:
            raise keyring.errors.KeyringLocked("locked")

    keyring.set_keyring(_Locked())

    response = _off(client)

    assert response.status_code == 503
    assert "locked" in response.json()["detail"]
    assert "sk-plain" not in response.text
    assert file_store_path().is_file()
    assert client.get("/api/settings/secret-storage").json()["file_store_enabled"] is True


def test_move_explicitly_requested_without_a_keychain_is_refused(
    client: TestClient, no_keychain: object
) -> None:
    _enable_file_store_and_save_a_key(client)
    response = _off(client, file_key_action="move")
    assert response.status_code == 503
    assert file_store_path().is_file()


def test_turning_off_with_no_key_file_just_works(client: TestClient, no_keychain: object) -> None:
    client.put("/api/settings/secret-storage", json={"allow_file_store": True})
    body = _off(client).json()
    assert (body["file_key_outcome"], body["file_store_enabled"]) == ("none", False)


def test_a_file_key_action_makes_no_sense_when_turning_the_file_on(
    client: TestClient, no_keychain: object
) -> None:
    response = client.put(
        "/api/settings/secret-storage", json={"allow_file_store": True, "file_key_action": "keep"}
    )
    assert response.status_code == 400


def test_turning_off_a_corrupt_key_file_can_still_delete_it(
    client: TestClient, no_keychain: object
) -> None:
    client.put("/api/settings/secret-storage", json={"allow_file_store": True})
    file_store_path().parent.mkdir(parents=True)
    file_store_path().write_text("not [toml", encoding="utf-8")
    assert _off(client).status_code == 409  # still needs a choice
    assert _off(client, file_key_action="delete").status_code == 200
    assert not file_store_path().exists()


# --- the Settings page load does not read the pre-rename keychain entry ----------------------


def test_status_and_settings_reads_do_not_probe_the_legacy_service(client: TestClient) -> None:
    gets: list[str] = []

    class _Spy(_Recording):
        def get_password(self, service: str, username: str) -> str | None:
            gets.append(service)
            return super().get_password(service, username)

    fake = _Spy()
    keyring.set_keyring(fake)
    saved = client.get("/api/settings").json()
    saved["llm"]["base_url"] = "https://api.example.com"
    client.put("/api/settings", json=saved)
    fake.store[("spend-analyzer", "llm:api.example.com")] = "old"
    gets.clear()

    status = client.get("/api/settings/secret-storage").json()
    assert client.get("/api/settings").json()["llm"]["has_key"] is False
    assert "spend-analyzer" not in gets  # nothing that could make macOS ask for permission
    assert (status["key_source"], status["key_legacy"], status["legacy_checked"]) == (
        "none",
        False,
        False,
    )

    explicit = client.get("/api/settings/secret-storage?check_legacy=true").json()
    assert "spend-analyzer" in gets  # only on request
    assert (explicit["key_source"], explicit["key_legacy"], explicit["legacy_checked"]) == (
        "keychain",
        True,
        True,
    )
    assert fake.writes == []
