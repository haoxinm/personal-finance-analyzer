"""`pfa llm ...` (network-free)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_finance_analyzer import config as config_module
from personal_finance_analyzer.cli import app
from personal_finance_analyzer.config import load_settings
from tests.llm_fakes import FakeLLM, install

runner = CliRunner()

_REPLY = (
    '{"items": [{"id": 1, "category": "grocery", "subcategory": "grocery_stores",'
    ' "merchant_canonical": "Test Merchant", "confidence": 0.9, "is_online_store": false}]}'
)


class _Keyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.store[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        import keyring.errors

        if (service, account) not in self.store:
            raise keyring.errors.PasswordDeleteError("nope")
        del self.store[(service, account)]


@pytest.fixture
def kr(monkeypatch: pytest.MonkeyPatch) -> _Keyring:
    fake = _Keyring()
    monkeypatch.setattr(config_module.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(config_module.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(config_module.keyring, "delete_password", fake.delete_password)
    monkeypatch.delenv("PFA_LLM_API_KEY", raising=False)
    return fake


def test_set_persists_to_config_toml_and_prints_mode(home: Path) -> None:
    result = runner.invoke(
        app,
        [
            "llm", "set", "--base-url", "https://api.example.com", "--model", "m",
            "--format", "openai_responses", "--temperature", "0.3", "--max-tokens", "500",
            "--extra-body", '{"top_k": 2}', "--extra-header", "X-A=b", "--enable",
            "--default-extractor", "llm", "--batch-size", "10", "--no-structured-output",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "mode: remote" in result.output
    assert "https://api.example.com/v1/responses" in result.output
    assert "will be sent to api.example.com" in result.output
    cfg = load_settings()
    assert cfg.llm.enabled and cfg.llm.model == "m" and cfg.llm.api_format == "openai_responses"
    assert cfg.llm.temperature == 0.3 and cfg.llm.max_tokens == 500
    assert cfg.llm.extra_body == {"top_k": 2} and cfg.llm.extra_headers == {"X-A": "b"}
    assert cfg.llm.batch_size == 10 and cfg.llm.structured_output is False
    assert cfg.ingest.default_extractor == "llm"
    assert "llm" in (home / "config.toml").read_text()


def test_set_only_changes_given_options(home: Path) -> None:
    runner.invoke(app, ["llm", "set", "--base-url", "http://localhost:1", "--model", "a"])
    runner.invoke(app, ["llm", "set", "--temperature", "0.5"])
    runner.invoke(app, ["llm", "set", "--temperature", "none", "--model", "b"])
    cfg = load_settings().llm
    assert cfg.base_url == "http://localhost:1" and cfg.model == "b" and cfg.temperature is None


def test_extra_headers_accumulate_and_clear(home: Path) -> None:
    runner.invoke(app, ["llm", "set", "--extra-header", "A=1"])
    runner.invoke(app, ["llm", "set", "--extra-header", "B=2"])
    assert load_settings().llm.extra_headers == {"A": "1", "B": "2"}
    runner.invoke(app, ["llm", "set", "--clear-extra-headers"])
    assert load_settings().llm.extra_headers == {}


def test_preset_fills_url_and_format_unless_explicit(home: Path) -> None:
    result = runner.invoke(app, ["llm", "set", "--preset", "anthropic", "--model", "m", "--enable"])
    assert result.exit_code == 0, result.output
    cfg = load_settings().llm
    assert cfg.base_url == "https://api.anthropic.com" and cfg.api_format == "anthropic_messages"
    runner.invoke(app, ["llm", "set", "--preset", "ollama", "--base-url", "http://localhost:9"])
    cfg = load_settings().llm
    assert cfg.base_url == "http://localhost:9" and cfg.api_format == "openai_chat"
    result = runner.invoke(app, ["llm", "set", "--preset", "ollama"])
    assert "mode: local" in result.output
    assert "WARNING" not in result.output


def test_invalid_inputs_are_rejected(home: Path) -> None:
    for args in (
        ["--format", "bogus"],
        ["--preset", "bogus"],
        ["--extra-body", "[1]"],
        ["--extra-body", "{bad"],
        ["--extra-header", "novalue"],
        ["--temperature", "hot"],
        ["--default-extractor", "x"],
    ):
        result = runner.invoke(app, ["llm", "set", *args])
        assert result.exit_code != 0, args
    assert not (home / "config.toml").exists()


def test_enable_disable_and_show(home: Path, kr: _Keyring) -> None:
    runner.invoke(app, ["llm", "set", "--base-url", "http://localhost:11434", "--model", "m"])
    assert "mode: none" in runner.invoke(app, ["llm", "show"]).output
    assert "mode: local" in runner.invoke(app, ["llm", "enable"]).output
    shown = runner.invoke(app, ["llm", "show"]).output
    assert "mode: local" in shown
    assert 'base_url: "http://localhost:11434"' in shown
    assert "api key on file: no" in shown
    assert "default_extractor: auto" in shown
    assert str(home / "config.toml") in shown
    assert "mode: none" in runner.invoke(app, ["llm", "disable"]).output
    assert load_settings().llm.enabled is False


def test_enable_without_base_url_explains(home: Path) -> None:
    result = runner.invoke(app, ["llm", "enable"])
    assert result.exit_code == 0
    assert "not usable yet" in result.output


def test_enable_with_base_url_and_no_model_is_usable(home: Path) -> None:
    runner.invoke(app, ["llm", "set", "--base-url", "http://127.0.0.1:8000"])
    result = runner.invoke(app, ["llm", "enable"])
    assert result.exit_code == 0
    assert "mode: local" in result.output
    assert "not usable yet" not in result.output


def test_set_key_from_stdin_and_clear_key(home: Path, kr: _Keyring) -> None:
    secret = "sk-secret-123"
    result = runner.invoke(
        app, ["llm", "set-key", "--base-url", "https://api.example.com"], input=secret + "\n"
    )
    assert result.exit_code == 0, result.output
    assert secret not in result.output
    assert kr.store[("personal-finance-analyzer", "llm:api.example.com")] == secret
    assert not (home / "config.toml").exists()

    runner.invoke(app, ["llm", "set", "--base-url", "https://api.example.com"])
    assert "api key on file: yes" in runner.invoke(app, ["llm", "show"]).output
    assert secret not in runner.invoke(app, ["llm", "show"]).output

    result = runner.invoke(app, ["llm", "clear-key"])
    assert result.exit_code == 0
    assert not kr.store


def test_set_key_without_any_base_url_fails(home: Path, kr: _Keyring) -> None:
    result = runner.invoke(app, ["llm", "set-key"], input="k\n")
    assert result.exit_code != 0
    assert not kr.store


def test_test_command(home: Path, kr: _Keyring, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakeLLM(default=_REPLY))
    result = runner.invoke(app, ["llm", "test"])
    assert result.exit_code == 1
    assert "turned off" in result.output
    assert not fake.requests

    runner.invoke(
        app, ["llm", "set", "--base-url", "https://api.example.com", "--model", "m", "--enable"]
    )
    result = runner.invoke(app, ["llm", "test"])
    assert result.exit_code == 0, result.output
    assert "Connected to https://api.example.com/v1/chat/completions" in result.output
    assert len(fake.requests) == 1


def test_test_command_failure_exits_1(
    home: Path, kr: _Keyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, FakeLLM(default=(500, {"error": "boom"})))
    runner.invoke(
        app, ["llm", "set", "--base-url", "https://api.example.com", "--model", "m", "--enable"]
    )
    result = runner.invoke(app, ["llm", "test"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_doctor_skips_llm_when_disabled(home: Path) -> None:
    result = runner.invoke(app, ["doctor"])
    assert "SKIP  LLM connectivity (disabled" in result.output


def test_doctor_checks_llm_when_enabled(
    home: Path, kr: _Keyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, FakeLLM(default=_REPLY))
    runner.invoke(
        app, ["llm", "set", "--base-url", "http://localhost:1", "--model", "m", "--enable"]
    )
    result = runner.invoke(app, ["doctor"])
    assert "PASS  LLM connectivity" in result.output


def test_help_mentions_llm() -> None:
    assert "llm" in runner.invoke(app, ["--help"]).output


def test_tax_llm_fallback_flag(home: Path) -> None:
    assert load_settings().ingest.tax_llm_fallback is False
    result = runner.invoke(app, ["llm", "set", "--tax-llm-fallback"])
    assert result.exit_code == 0, result.output
    assert load_settings().ingest.tax_llm_fallback is True
    assert "[ingest] tax_llm_fallback: true" in runner.invoke(app, ["llm", "show"]).output
    runner.invoke(app, ["llm", "set", "--no-tax-llm-fallback"])
    assert load_settings().ingest.tax_llm_fallback is False


def test_disabling_the_llm_clears_the_tax_fallback_opt_in(home: Path) -> None:
    runner.invoke(app, ["llm", "set", "--base-url", "http://localhost:11434", "--model", "m"])
    runner.invoke(app, ["llm", "enable"])
    runner.invoke(app, ["llm", "set", "--tax-llm-fallback"])
    assert load_settings().ingest.tax_llm_fallback is True
    runner.invoke(app, ["llm", "disable"])
    assert load_settings().ingest.tax_llm_fallback is False
    runner.invoke(app, ["llm", "enable"])
    assert load_settings().ingest.tax_llm_fallback is False
    runner.invoke(app, ["llm", "set", "--tax-llm-fallback"])
    runner.invoke(app, ["llm", "set", "--disable"])
    assert load_settings().ingest.tax_llm_fallback is False
