from __future__ import annotations

from pathlib import Path

import keyring.errors
import pytest

from personal_finance_analyzer import config as config_module
from personal_finance_analyzer.config import (
    IngestConfig,
    LLMConfig,
    PrivacyConfig,
    ServerConfig,
    Settings,
    delete_api_key,
    get_api_key,
    get_llm_api_key,
    load_settings,
    save_settings,
    set_api_key,
)


def test_round_trip_preserves_all_fields(home: Path) -> None:
    settings = Settings(
        llm=LLMConfig(
            mode="remote",
            provider="anthropic",
            model="claude-x",
            api_base="",
            batch_size=25,
            timeout_s=30,
            confidence_threshold=0.9,
        ),
        privacy=PrivacyConfig(
            store_pdf_copies=False, store_extract_cache=True, pii_terms=("Alice", "Bob")
        ),
        ingest=IngestConfig(
            always_confirm_extractor=True,
            date_format_hints=("%Y-%m-%d",),
            default_currency="EUR",
        ),
        server=ServerConfig(port=9999),
    )
    path = home / "config.toml"
    save_settings(settings, path)
    loaded = load_settings(path)
    assert loaded == settings


def test_load_settings_defaults_when_file_absent(home: Path) -> None:
    loaded = load_settings(home / "does-not-exist.toml")
    assert loaded == Settings()


def test_env_override_wins_over_toml(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = home / "config.toml"
    save_settings(
        Settings(llm=LLMConfig(base_url="http://localhost:1", model="m", enabled=False)), path
    )
    monkeypatch.setenv("PFA_LLM__MODE", "local")
    loaded = load_settings(path)
    assert loaded.llm.mode == "local"


def test_legacy_env_prefix_is_still_accepted(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEND_ANALYZER_LLM__ENABLED", "true")
    monkeypatch.setenv("SPEND_ANALYZER_LLM__MODEL", "legacy-model")
    monkeypatch.setenv("SPEND_ANALYZER_TAX__AUTO_UPDATE_PARAMS", "true")
    loaded = load_settings(home / "config.toml")
    assert loaded.llm.enabled is True
    assert loaded.llm.model == "legacy-model"
    assert loaded.tax.auto_update_params is True


def test_new_env_prefix_wins_over_the_legacy_one(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPEND_ANALYZER_LLM__MODEL", "legacy-model")
    monkeypatch.setenv("SPEND_ANALYZER_LLM__ENABLED", "true")
    monkeypatch.setenv("PFA_LLM__MODEL", "new-model")
    loaded = load_settings(home / "config.toml")
    assert loaded.llm.model == "new-model"
    assert loaded.llm.enabled is True  # a field only the legacy prefix sets still applies


def test_legacy_llm_api_key_env_is_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = LLMConfig(base_url="https://api.example.com/v1", model="m")
    monkeypatch.delenv("PFA_LLM_API_KEY", raising=False)
    monkeypatch.setenv("SPEND_ANALYZER_LLM_API_KEY", "legacy-key")
    assert get_llm_api_key(cfg) == "legacy-key"
    monkeypatch.setenv("PFA_LLM_API_KEY", "new-key")
    assert get_llm_api_key(cfg) == "new-key"


def test_save_settings_is_atomic_write(home: Path) -> None:
    path = home / "config.toml"
    save_settings(Settings(), path)
    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()


class _FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.store[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        # Like a real backend: deleting an entry that is not there is an error.
        if (service, account) not in self.store:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.store[(service, account)]


def test_api_key_never_touches_config_or_log(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeKeyring()
    monkeypatch.setattr(config_module.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(config_module.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(config_module.keyring, "delete_password", fake.delete_password)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    set_api_key("anthropic", "sk-ant-super-secret-value")
    assert get_api_key("anthropic") == "sk-ant-super-secret-value"

    path = home / "config.toml"
    save_settings(Settings(llm=LLMConfig(provider="anthropic")), path)
    assert "sk-ant-super-secret-value" not in path.read_text()

    delete_api_key("anthropic")
    assert get_api_key("anthropic") is None


def _install_fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    monkeypatch.setattr(config_module.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(config_module.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(config_module.keyring, "delete_password", fake.delete_password)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return fake


def test_api_key_saved_under_the_old_service_is_moved_on_first_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_keyring(monkeypatch)
    fake.store[("spend-analyzer", "anthropic")] = "old-key"

    assert get_api_key("anthropic") == "old-key"
    assert fake.store == {("personal-finance-analyzer", "anthropic"): "old-key"}
    assert get_api_key("anthropic") == "old-key"  # now served from the new service


def test_new_keychain_entry_wins_and_leaves_the_old_one_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_keyring(monkeypatch)
    fake.store[("spend-analyzer", "anthropic")] = "old-key"
    fake.store[("personal-finance-analyzer", "anthropic")] = "new-key"
    assert get_api_key("anthropic") == "new-key"
    assert fake.store[("spend-analyzer", "anthropic")] == "old-key"


def test_old_keychain_entry_still_works_when_it_cannot_be_copied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_keyring(monkeypatch)
    fake.store[("spend-analyzer", "anthropic")] = "old-key"

    def refuse(service: str, account: str, value: str) -> None:
        raise keyring.errors.KeyringError("read-only keychain")

    monkeypatch.setattr(config_module.keyring, "set_password", refuse)
    assert get_api_key("anthropic") == "old-key"
    assert fake.store == {("spend-analyzer", "anthropic"): "old-key"}  # nothing deleted


def test_set_and_delete_api_key_also_clear_the_old_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_keyring(monkeypatch)
    fake.store[("spend-analyzer", "anthropic")] = "old-key"
    set_api_key("anthropic", "fresh")
    assert fake.store == {("personal-finance-analyzer", "anthropic"): "fresh"}

    fake.store[("spend-analyzer", "anthropic")] = "stale-again"
    delete_api_key("anthropic")
    assert fake.store == {}


def test_get_api_key_prefers_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "env-value")
    assert get_api_key("openai") == "env-value"


# ---- unified LLMConfig ------------------------------------------------------------------------


def test_round_trip_new_fields(home: Path) -> None:
    for temperature in (None, 0.0, 0.4):
        settings = Settings(
            llm=LLMConfig(
                enabled=True,
                base_url="https://api.example.com/v1",
                api_format="anthropic_messages",
                path="/custom",
                model="m",
                temperature=temperature,
                max_tokens=1234,
                reasoning_effort="high",
                extra_body={"top_p": 0.9, "nested": {"a": [1, 2]}},
                extra_headers={"X-Foo": "bar"},
                structured_output=False,
                parse_pages_per_request=3,
            ),
            ingest=IngestConfig(default_extractor="llm"),
        )
        path = home / "config.toml"
        save_settings(settings, path)
        assert "temperature" not in path.read_text() or temperature is not None
        assert load_settings(path) == settings


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        (
            {"mode": "remote", "provider": "anthropic", "model": "anthropic/claude-x"},
            (True, "https://api.anthropic.com", "anthropic_messages", "claude-x"),
        ),
        (
            {"mode": "remote", "provider": "openai", "model": "openai/gpt-x"},
            (True, "https://api.openai.com", "openai_chat", "gpt-x"),
        ),
        (
            {"mode": "local", "provider": "ollama", "model": "ollama/llama3"},
            (True, "http://localhost:11434", "openai_chat", "llama3"),
        ),
        (
            {"mode": "local", "provider": "lm_studio", "model": "lm_studio/qwen"},
            (True, "http://localhost:1234", "openai_chat", "qwen"),
        ),
        (
            {"mode": "local", "provider": "ollama", "api_base": "http://box:9000", "model": "x"},
            (True, "http://box:9000", "openai_chat", "x"),
        ),
        (
            {"mode": "none", "provider": "anthropic", "model": "claude-x"},
            (False, "https://api.anthropic.com", "anthropic_messages", "claude-x"),
        ),
        ({"mode": "none"}, (False, "", "openai_chat", "")),
    ],
)
def test_legacy_migration(legacy: dict[str, str], expected: tuple[bool, str, str, str]) -> None:
    cfg = LLMConfig(**legacy)
    assert (cfg.enabled, cfg.base_url, cfg.api_format, cfg.model) == expected


def test_legacy_toml_file_loads(home: Path) -> None:
    path = home / "config.toml"
    path.write_text('[llm]\nmode = "local"\nprovider = "ollama"\nmodel = "ollama/llama3"\n')
    cfg = load_settings(path).llm
    assert cfg.mode == "local"
    assert cfg.model == "llama3"
    assert cfg.resolved_url == "http://localhost:11434/v1/chat/completions"


def test_legacy_mode_none_is_disabled() -> None:
    cfg = LLMConfig(mode="none")
    assert cfg.enabled is False
    assert cfg.mode == "none"
    assert not cfg.is_configured


@pytest.mark.parametrize(
    ("base_url", "mode"),
    [
        ("http://localhost:11434", "local"),
        ("http://LOCALHOST:11434", "local"),
        ("http://127.0.0.1:1234", "local"),
        ("http://127.0.1.1", "local"),
        ("http://[::1]:8080", "local"),
        ("https://api.anthropic.com", "remote"),
        ("http://192.168.1.5:11434", "remote"),
        ("https://localhost.evil.example.com", "remote"),
    ],
)
def test_mode_derivation(base_url: str, mode: str) -> None:
    cfg = LLMConfig(enabled=True, base_url=base_url, model="m")
    assert cfg.mode == mode
    assert cfg.is_configured


def test_mode_none_when_disabled_or_incomplete() -> None:
    assert LLMConfig(enabled=False, base_url="http://localhost:1", model="m").mode == "none"
    assert LLMConfig(enabled=True, base_url="", model="m").mode == "none"
    assert LLMConfig(enabled=True, base_url="  ", model="m").mode == "none"


def test_blank_model_is_still_configured() -> None:
    local = LLMConfig(enabled=True, base_url="http://127.0.0.1:8000", model="")
    assert (local.mode, local.is_configured) == ("local", True)
    remote = LLMConfig(enabled=True, base_url="https://api.example.com", model="")
    assert (remote.mode, remote.is_configured) == ("remote", True)


@pytest.mark.parametrize(
    ("kwargs", "url"),
    [
        ({"base_url": "https://h.com"}, "https://h.com/v1/chat/completions"),
        ({"base_url": "https://h.com/"}, "https://h.com/v1/chat/completions"),
        ({"base_url": "https://h.com/v1"}, "https://h.com/v1/chat/completions"),
        (
            {"base_url": "https://h.com/v1/", "api_format": "openai_responses"},
            "https://h.com/v1/responses",
        ),
        (
            {"base_url": "https://h.com/v1", "api_format": "anthropic_messages"},
            "https://h.com/v1/messages",
        ),
        ({"base_url": "https://h.com", "path": "/api/chat"}, "https://h.com/api/chat"),
        ({"base_url": "https://h.com/", "path": "api/chat"}, "https://h.com/api/chat"),
        ({"base_url": "https://h.com/v1", "path": "/v1/custom"}, "https://h.com/v1/v1/custom"),
    ],
)
def test_resolved_url(kwargs: dict[str, str], url: str) -> None:
    assert LLMConfig(**kwargs).resolved_url == url


def test_key_account() -> None:
    assert LLMConfig(base_url="https://API.Anthropic.com/v1").key_account == "llm:api.anthropic.com"
    assert LLMConfig(base_url="http://localhost:11434").key_account == "llm:localhost:11434"
    assert LLMConfig().key_account == ""


def test_env_override_enabled(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = home / "config.toml"
    save_settings(Settings(llm=LLMConfig(base_url="http://localhost:1", model="m")), path)
    monkeypatch.setenv("PFA_LLM__ENABLED", "true")
    loaded = load_settings(path).llm
    assert loaded.enabled is True
    assert loaded.mode == "local"


def test_env_override_legacy_mode(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = home / "config.toml"
    save_settings(Settings(llm=LLMConfig(base_url="http://localhost:1", model="m")), path)
    monkeypatch.setenv("PFA_LLM__MODE", "local")
    assert load_settings(path).llm.enabled is True
    monkeypatch.setenv("PFA_LLM__MODE", "none")
    assert load_settings(path).llm.enabled is False


def test_env_override_optional_and_dict_fields(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_LLM__TEMPERATURE", "0.25")
    monkeypatch.setenv("PFA_LLM__MAX_TOKENS", "100")
    monkeypatch.setenv("PFA_LLM__EXTRA_BODY", '{"top_p": 0.5}')
    cfg = load_settings(home / "none.toml").llm
    assert cfg.temperature == 0.25
    assert cfg.max_tokens == 100
    assert cfg.extra_body == {"top_p": 0.5}


def test_get_llm_api_key_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_LLM_API_KEY", "from-env")
    cfg = LLMConfig(base_url="https://api.example.com", model="m")
    assert get_llm_api_key(cfg) == "from-env"


def test_get_llm_api_key_from_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKeyring()
    monkeypatch.setattr(config_module.keyring, "get_password", fake.get_password)
    monkeypatch.delenv("PFA_LLM_API_KEY", raising=False)
    cfg = LLMConfig(base_url="https://api.example.com", model="m")
    assert get_llm_api_key(cfg) is None
    fake.store[("personal-finance-analyzer", "llm:api.example.com")] = "kc-secret"
    assert get_llm_api_key(cfg) == "kc-secret"
    other = LLMConfig(base_url="https://other.example.com", model="m")
    assert get_llm_api_key(other) is None  # a key is never reused for another host


def test_get_llm_api_key_without_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PFA_LLM_API_KEY", raising=False)
    assert get_llm_api_key(LLMConfig()) is None


def test_keyring_error_reads_as_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(service: str, account: str) -> str:
        raise keyring.errors.KeyringError("no backend")

    monkeypatch.setattr(config_module.keyring, "get_password", _raise)
    monkeypatch.delenv("PFA_LLM_API_KEY", raising=False)
    assert get_api_key("llm:api.example.com") is None
    assert get_llm_api_key(LLMConfig(base_url="https://api.example.com", model="m")) is None


def test_api_key_env_var_for_account_with_symbols_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_module.keyring, "get_password", lambda s, a: None)
    assert get_api_key("llm:host:1234") is None
