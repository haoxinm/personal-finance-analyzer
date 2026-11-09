"""`/api/settings*` (A4, I7): never returns key material."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from personal_finance_analyzer import config as config_module
from tests.llm_fakes import FakeLLM, install


class _FakeKeyring:
    """Mirrors `tests/core/test_config.py`'s fake so this file never touches a real OS
    keychain (I7's `config.set_api_key`/`get_api_key`/`delete_api_key` call `keyring.*`
    directly)."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.store[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        import keyring.errors

        if (service, account) not in self.store:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.store[(service, account)]


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    monkeypatch.setattr(config_module.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(config_module.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(config_module.keyring, "delete_password", fake.delete_password)
    monkeypatch.delenv("PFA_LLM_API_KEY", raising=False)
    return fake


def test_get_settings_has_no_key_material(client: TestClient) -> None:
    response = client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()
    assert "has_key" in body["llm"]
    assert "api_key" not in body["llm"]
    assert "key" not in body["llm"]


def test_put_settings_round_trips(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    body["llm"]["batch_size"] = 25
    response = client.put("/api/settings", json=body)
    assert response.status_code == 200
    assert response.json()["llm"]["batch_size"] == 25

    refetched = client.get("/api/settings").json()
    assert refetched["llm"]["batch_size"] == 25


def test_put_settings_preserves_config_toml_only_tax_pdf_flag(client: TestClient) -> None:
    from personal_finance_analyzer.config import load_settings, save_settings

    saved = load_settings()
    save_settings(
        saved.model_copy(
            update={"privacy": saved.privacy.model_copy(update={"store_tax_pdf_copies": True})}
        )
    )
    body = client.get("/api/settings").json()
    assert "store_tax_pdf_copies" not in body["privacy"]
    assert client.put("/api/settings", json=body).status_code == 200
    assert load_settings().privacy.store_tax_pdf_copies is True


def test_put_settings_preserves_the_documents_folder_setting(client: TestClient) -> None:
    """`[storage] documents_dir` names a place on disk: config.toml-only, never in the API, and a
    save from the Settings screen must neither drop nor change it."""
    from personal_finance_analyzer.config import StorageConfig, load_settings, save_settings

    saved = load_settings()
    save_settings(saved.model_copy(update={"storage": StorageConfig(documents_dir="/mnt/nas/pfa")}))
    body = client.get("/api/settings").json()
    assert "storage" not in body and "documents_dir" not in str(body)
    assert client.put("/api/settings", json=body).status_code == 200
    assert load_settings().storage.documents_dir == "/mnt/nas/pfa"


def test_the_documents_env_var_is_never_written_to_config_toml(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from personal_finance_analyzer.config import load_file_settings

    monkeypatch.setenv("PFA_DOCUMENTS_DIR", str(tmp_path / "from-env"))
    body = client.get("/api/settings").json()
    assert client.put("/api/settings", json=body).status_code == 200
    assert load_file_settings().storage.documents_dir == ""


def test_test_llm_reports_disabled_when_mode_is_none(client: TestClient) -> None:
    response = client.post("/api/settings/test-llm")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False


_REPLY = (
    '{"items": [{"id": 1, "category": "grocery", "subcategory": "grocery_stores",'
    ' "merchant_canonical": "Test Merchant", "confidence": 0.9, "is_online_store": false}]}'
)
_SECRET = "sk-ant-super-secret-value"
_ANTHROPIC = "https://api.anthropic.com"
_ACCOUNT = ("personal-finance-analyzer", "llm:api.anthropic.com")


def _draft(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "enabled": True,
        "base_url": "https://api.example.com",
        "api_format": "openai_chat",
        "model": "m",
    }
    base.update(kw)
    return base


def test_get_settings_reports_derived_fields(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    body["llm"].update(enabled=True, base_url="http://localhost:11434", model="llama3")
    saved = client.put("/api/settings", json=body).json()["llm"]
    assert saved["mode"] == "local"
    assert saved["resolved_url"] == "http://localhost:11434/v1/chat/completions"
    refetched = client.get("/api/settings").json()["llm"]
    assert refetched["mode"] == "local"
    assert refetched["resolved_url"] == saved["resolved_url"]


def test_put_ignores_derived_fields_and_round_trips_values(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    body["llm"].update(
        enabled=True,
        base_url="https://api.example.com",
        model="m",
        mode="local",
        resolved_url="bogus",
        has_key=True,
        temperature=0.25,
        extra_body={"top_k": 3},
        extra_headers={"X-A": "b"},
    )
    out = client.put("/api/settings", json=body).json()["llm"]
    assert out["mode"] == "remote"
    assert out["resolved_url"] == "https://api.example.com/v1/chat/completions"
    assert out["has_key"] is False
    assert out["temperature"] == 0.25
    assert out["extra_body"] == {"top_k": 3}
    assert out["extra_headers"] == {"X-A": "b"}

    body["llm"]["temperature"] = None
    assert client.put("/api/settings", json=body).json()["llm"]["temperature"] is None
    assert client.get("/api/settings").json()["llm"]["temperature"] is None


def test_test_llm_draft_works_while_saved_config_is_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM(default=_REPLY))
    assert client.get("/api/settings").json()["llm"]["mode"] == "none"
    response = client.post("/api/settings/test-llm", json={"llm": _draft()})
    body = response.json()
    assert body["ok"] is True, body
    assert fake.urls == ["https://api.example.com/v1/chat/completions"]
    assert "Connected to https://api.example.com/v1/chat/completions (remote)" in body["detail"]
    assert body["mode"] == "remote"
    assert body["resolved_url"] == "https://api.example.com/v1/chat/completions"
    assert "'none'" not in (body["detail"] or "")


def test_test_llm_draft_uses_key_for_draft_host(
    client: TestClient, fake_keyring: _FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_keyring.store[("personal-finance-analyzer", "llm:api.example.com")] = "key-for-example"
    fake = install(monkeypatch, FakeLLM(default=_REPLY))
    client.post("/api/settings/test-llm", json={"llm": _draft()})
    assert fake.headers
    assert "key-for-example" in str(fake.headers[0])


def test_test_llm_disabled_draft_explains_why(client: TestClient) -> None:
    body = client.post("/api/settings/test-llm", json={"llm": _draft(enabled=False)}).json()
    assert body["ok"] is False
    assert "turned off" in body["detail"]
    assert "Use an LLM" in body["detail"]

    body = client.post("/api/settings/test-llm", json={"llm": _draft(base_url="", model="")}).json()
    assert "no address" in body["detail"]
    assert "no model" not in body["detail"]


def test_test_llm_with_blank_model_reaches_the_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM(default=_REPLY))
    draft = _draft(base_url="http://127.0.0.1:8000", model="", api_format="openai_responses")
    body = client.post("/api/settings/test-llm", json={"llm": draft}).json()
    assert body["ok"] is True, body
    assert body["mode"] == "local"
    assert len(fake.requests) == 1
    assert "model" not in fake.requests[0]


def test_test_llm_without_body_tests_saved_settings(client: TestClient) -> None:
    body = client.post("/api/settings/test-llm").json()
    assert body["ok"] is False
    assert body["mode"] == "none"


def test_put_api_key_stores_it_and_never_echoes_it(
    client: TestClient, fake_keyring: _FakeKeyring
) -> None:
    response = client.put(
        "/api/settings/api-key", json={"base_url": _ANTHROPIC, "api_key": _SECRET}
    )
    assert response.status_code == 204
    assert response.text == ""
    assert _SECRET not in response.headers.values()
    assert fake_keyring.store[_ACCOUNT] == _SECRET
    assert _SECRET not in str(client.get("/api/settings").json())


def test_put_api_key_defaults_to_saved_base_url(
    client: TestClient, fake_keyring: _FakeKeyring
) -> None:
    body = client.get("/api/settings").json()
    body["llm"].update(enabled=True, base_url=_ANTHROPIC, model="m")
    client.put("/api/settings", json=body)
    assert client.put("/api/settings/api-key", json={"api_key": _SECRET}).status_code == 204
    assert fake_keyring.store[_ACCOUNT] == _SECRET
    assert client.get("/api/settings").json()["llm"]["has_key"] is True


def test_put_api_key_without_any_base_url_is_400(
    client: TestClient, fake_keyring: _FakeKeyring
) -> None:
    response = client.put("/api/settings/api-key", json={"api_key": _SECRET})
    assert response.status_code == 400
    assert "base URL" in response.json()["detail"]
    assert not fake_keyring.store


def test_put_api_key_is_never_logged(
    client: TestClient, fake_keyring: _FakeKeyring, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = client.put(
            "/api/settings/api-key", json={"base_url": _ANTHROPIC, "api_key": _SECRET}
        )
    assert response.status_code == 204
    for record in caplog.records:
        assert _SECRET not in record.getMessage()


def test_put_api_key_never_reaches_config_toml(
    client: TestClient, fake_keyring: _FakeKeyring, home: Path
) -> None:
    client.put("/api/settings/api-key", json={"base_url": _ANTHROPIC, "api_key": _SECRET})
    settings_body = client.get("/api/settings").json()
    settings_body["llm"]["base_url"] = _ANTHROPIC
    client.put("/api/settings", json=settings_body)

    config_path = home / "config.toml"
    if config_path.exists():
        assert _SECRET not in config_path.read_text()


def test_delete_api_key_clears_it(client: TestClient, fake_keyring: _FakeKeyring) -> None:
    client.put("/api/settings/api-key", json={"base_url": _ANTHROPIC, "api_key": _SECRET})
    assert _ACCOUNT in fake_keyring.store

    response = client.delete("/api/settings/api-key", params={"base_url": _ANTHROPIC})
    assert response.status_code == 204
    assert _ACCOUNT not in fake_keyring.store


def test_delete_api_key_is_idempotent_when_absent(
    client: TestClient, fake_keyring: _FakeKeyring
) -> None:
    response = client.delete("/api/settings/api-key", params={"base_url": "https://nowhere.test"})
    assert response.status_code == 204


def test_api_key_routes_require_the_token(client: TestClient) -> None:
    put_response = client.put(
        "/api/settings/api-key",
        json={"base_url": _ANTHROPIC, "api_key": _SECRET},
        headers={"X-PFA-Token": "wrong-token"},
    )
    assert put_response.status_code == 403

    delete_response = client.delete(
        "/api/settings/api-key",
        params={"base_url": _ANTHROPIC},
        headers={"X-PFA-Token": "wrong-token"},
    )
    assert delete_response.status_code == 403


def test_pii_test_masks_sample_text_with_draft_terms(client: TestClient) -> None:
    r = client.post(
        "/api/settings/pii-test",
        json={"text": "Jane Doe 312.555.0100 ok", "terms": ["doe, jane", "(312) 555-0100"]},
    )
    assert r.status_code == 200
    assert r.json() == {"redacted_text": "[NAME] [PHONE] ok", "count": 2}


def test_pii_test_defaults_to_saved_terms(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PFA_PRIVACY__PII_TERMS", "Alice Example")
    r = client.post("/api/settings/pii-test", json={"text": "hi ALICE  example"})
    assert r.json() == {"redacted_text": "hi [NAME]", "count": 1}
    r = client.post("/api/settings/pii-test", json={"text": "hi ALICE", "terms": []})
    assert r.json() == {"redacted_text": "hi ALICE", "count": 0}


def test_tax_llm_fallback_defaults_off_and_round_trips(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    assert body["ingest"]["tax_llm_fallback"] is False
    body["llm"].update(enabled=True, base_url="http://localhost:11434", model="m")
    body["ingest"]["tax_llm_fallback"] = True
    assert client.put("/api/settings", json=body).json()["ingest"]["tax_llm_fallback"] is True
    assert client.get("/api/settings").json()["ingest"]["tax_llm_fallback"] is True
    # an unrelated save must not drop it
    body = client.get("/api/settings").json()
    body["llm"]["batch_size"] = 12
    client.put("/api/settings", json=body)
    assert client.get("/api/settings").json()["ingest"]["tax_llm_fallback"] is True


def test_disabling_the_llm_clears_the_tax_fallback_opt_in(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    body["llm"].update(enabled=True, base_url="http://localhost:11434", model="m")
    body["ingest"]["tax_llm_fallback"] = True
    assert client.put("/api/settings", json=body).json()["ingest"]["tax_llm_fallback"] is True
    body["llm"]["enabled"] = False
    saved = client.put("/api/settings", json=body).json()
    assert saved["ingest"]["tax_llm_fallback"] is False
    body["llm"]["enabled"] = True  # re-enabling does not resume tax sending
    body["ingest"]["tax_llm_fallback"] = client.get("/api/settings").json()["ingest"][
        "tax_llm_fallback"
    ]
    assert client.put("/api/settings", json=body).json()["ingest"]["tax_llm_fallback"] is False
