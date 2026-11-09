"""`NAME` / `NAME_FILE` secrets, container detection, and the container-aware key guidance."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_finance_analyzer import config
from personal_finance_analyzer.core.container import (
    SecretFileError,
    env_flag,
    read_env_or_file,
    running_in_container,
)
from personal_finance_analyzer.secret_store import platform_guidance


def test_plain_variable_wins_and_is_not_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_T_SECRET", " spaced ")
    monkeypatch.delenv("PFA_T_SECRET_FILE", raising=False)
    assert read_env_or_file("PFA_T_SECRET") == " spaced "


def test_unset_and_empty_are_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PFA_T_SECRET", raising=False)
    monkeypatch.delenv("PFA_T_SECRET_FILE", raising=False)
    assert read_env_or_file("PFA_T_SECRET") is None
    monkeypatch.setenv("PFA_T_SECRET", "")
    assert read_env_or_file("PFA_T_SECRET") is None


def test_file_variable_reads_the_file_and_strips_only_trailing_newlines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = tmp_path / "secret"
    secret.write_bytes(b"  s3cret value \r\n\n")
    monkeypatch.delenv("PFA_T_SECRET", raising=False)
    monkeypatch.setenv("PFA_T_SECRET_FILE", str(secret))
    assert read_env_or_file("PFA_T_SECRET") == "  s3cret value "


def test_both_set_is_an_error_that_names_the_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PFA_T_SECRET", "x")
    monkeypatch.setenv("PFA_T_SECRET_FILE", str(tmp_path / "f"))
    with pytest.raises(SecretFileError, match="PFA_T_SECRET or PFA_T_SECRET_FILE"):
        read_env_or_file("PFA_T_SECRET")


def test_unreadable_file_is_an_error_without_the_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PFA_T_SECRET", raising=False)
    monkeypatch.setenv("PFA_T_SECRET_FILE", str(tmp_path / "missing"))
    with pytest.raises(SecretFileError, match="PFA_T_SECRET_FILE"):
        read_env_or_file("PFA_T_SECRET")


def test_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PFA_T_FLAG", raising=False)
    assert env_flag("PFA_T_FLAG") is False
    assert env_flag("PFA_T_FLAG", True) is True
    for raw in ("1", "TRUE", " yes ", "on"):
        monkeypatch.setenv("PFA_T_FLAG", raw)
        assert env_flag("PFA_T_FLAG") is True
    for raw in ("0", "False", "no", "off"):
        monkeypatch.setenv("PFA_T_FLAG", raw)
        assert env_flag("PFA_T_FLAG", True) is False


def test_container_detection_can_be_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_CONTAINER", "1")
    assert running_in_container() is True
    monkeypatch.setenv("PFA_CONTAINER", "0")
    assert running_in_container() is False


def test_guidance_in_a_container_points_at_the_key_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_CONTAINER", "1")
    text = platform_guidance()
    assert "PFA_LLM_API_KEY_FILE" in text
    assert "gnome-keyring" not in text
    # an explicit platform still gets that platform's advice
    assert "gnome-keyring" in platform_guidance("linux")
    monkeypatch.setenv("PFA_CONTAINER", "0")
    assert "PFA_LLM_API_KEY_FILE" not in platform_guidance("linux")


def test_llm_key_from_a_docker_secret_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key_file = tmp_path / "llm_api_key"
    key_file.write_text("sk-from-a-file\n", encoding="utf-8")
    monkeypatch.delenv("PFA_LLM_API_KEY", raising=False)
    monkeypatch.setenv("PFA_LLM_API_KEY_FILE", str(key_file))
    cfg = config.LLMConfig(enabled=True, base_url="https://api.example.com", model="m")
    assert config.get_llm_api_key(cfg) == "sk-from-a-file"
    status = config.secret_storage_status(cfg)
    assert status.key_source == "env"
    assert status.env_var == "PFA_LLM_API_KEY_FILE"


def test_llm_key_file_that_cannot_be_read_reads_as_no_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PFA_LLM_API_KEY", raising=False)
    monkeypatch.setenv("PFA_LLM_API_KEY_FILE", str(tmp_path / "missing"))
    cfg = config.LLMConfig(enabled=True, base_url="https://api.example.com", model="m")
    assert config.get_llm_api_key(cfg) is None
