"""Settings model, load/save, and secret storage (§3.10, P0-2).

`config.toml` holds every non-secret setting. Secrets (LLM provider API keys) live only in the
OS keychain via `keyring` (I7) — never in `config.toml`, never in the database, never logged.

Every other module receives a `Settings` object; nothing outside this module reads `config.toml`
or touches `keyring` directly.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import keyring
import keyring.errors
import tomli_w
from pydantic import BaseModel, ConfigDict, Field, model_validator

from personal_finance_analyzer.core.native_locks import KEYRING_LOCK
from personal_finance_analyzer.core.paths import config_path, ensure_home

#: keyring service name; the account within it is the provider name (e.g. "anthropic").
_KEYRING_SERVICE = "personal-finance-analyzer"


#: The wire formats `personal_finance_analyzer.llm.client` can speak.
ApiFormat = Literal["openai_chat", "openai_responses", "anthropic_messages"]

#: The request path each `ApiFormat` uses when `[llm] path` is left empty.
DEFAULT_PATHS: dict[str, str] = {
    "openai_chat": "/v1/chat/completions",
    "openai_responses": "/v1/responses",
    "anthropic_messages": "/v1/messages",
}

#: Hostnames that count as "this machine": an endpoint on one of these is a *local* LLM (nothing
#: leaves the computer); anything else is a *remote* LLM.
_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})

#: Legacy (`provider = ...`) presets, used only to migrate an old `config.toml` that predates
#: the unified `base_url`/`api_format` settings.
_LEGACY_PROVIDER_PRESETS: dict[str, tuple[str, str]] = {
    "anthropic": ("https://api.anthropic.com", "anthropic_messages"),
    "openai": ("https://api.openai.com", "openai_chat"),
    "ollama": ("http://localhost:11434", "openai_chat"),
    "lm_studio": ("http://localhost:1234", "openai_chat"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "openai_chat"),
}


def is_local_url(url: str) -> bool:
    """Whether ``url`` points at this machine (``localhost``, ``127.x.x.x``, ``::1``)."""
    host = (urlsplit(url).hostname or "").lower()
    return host in _LOCAL_HOSTNAMES or host.startswith("127.") or host.endswith(".localhost")


class LLMConfig(BaseModel):
    """One LLM endpoint, used for both classification and (opt-in, per statement) statement
    parsing. Local vs. remote is not a setting: it is derived from `base_url` (see `mode`)."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False  # D2: off by default — no LLM is ever called
    base_url: str = ""  # e.g. http://localhost:11434 or https://api.anthropic.com
    api_format: ApiFormat = "openai_chat"
    path: str = ""  # empty = DEFAULT_PATHS[api_format]
    model: str = ""
    temperature: float | None = None  # None = don't send (some newer models reject it)
    max_tokens: int | None = None  # None = provider default (anthropic_messages: 8192)
    reasoning_effort: str = ""  # "", minimal, low, medium, high, xhigh, max
    #: Extra top-level JSON fields merged into every request body (overrides the above).
    extra_body: dict[str, Any] = Field(default_factory=dict)
    #: Extra HTTP headers sent with every request (e.g. an OpenRouter `HTTP-Referer`).
    extra_headers: dict[str, str] = Field(default_factory=dict)
    #: Ask for JSON-schema-constrained output where the API format supports it. A server that
    #: rejects it (HTTP 400/422) is retried once without it, automatically.
    structured_output: bool = True
    batch_size: int = 50
    timeout_s: int = 120
    confidence_threshold: float = 0.7
    #: Statement parsing: at most this many pages per request (0 = all selected pages at once).
    parse_pages_per_request: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data: Any) -> Any:
        """Accept a pre-0.2 `[llm]` table (`mode`/`provider`/`api_base`) and translate it."""
        if not isinstance(data, dict) or not ({"mode", "provider", "api_base"} & data.keys()):
            return data
        data = dict(data)
        mode = data.pop("mode", None)
        provider = str(data.pop("provider", "") or "")
        api_base = str(data.pop("api_base", "") or "")
        if mode is not None and "enabled" not in data:
            data["enabled"] = mode != "none"
        preset = _LEGACY_PROVIDER_PRESETS.get(provider)
        if "base_url" not in data:
            data["base_url"] = api_base or (preset[0] if preset else "")
        if "api_format" not in data and preset is not None:
            data["api_format"] = preset[1]
        model = str(data.get("model", "") or "")
        if provider and model.startswith(f"{provider}/"):
            data["model"] = model[len(provider) + 1 :]  # litellm-style "ollama/llama3"
        return data

    @property
    def mode(self) -> Literal["none", "local", "remote"]:
        """`none` when disabled or no address; else `local`/`remote`, from `base_url`.

        The model is optional: single-model local servers (llama.cpp, vLLM) don't need one.
        """
        if not self.enabled or not self.base_url.strip():
            return "none"
        return "local" if is_local_url(self.base_url) else "remote"

    @property
    def is_configured(self) -> bool:
        return self.mode != "none"

    @property
    def resolved_url(self) -> str:
        """The full URL requests are POSTed to: `base_url` + `path` (or the format's default
        path), without doubling a `/v1` that `base_url` already ends with."""
        base = self.base_url.strip().rstrip("/")
        path = (self.path.strip() or DEFAULT_PATHS[self.api_format]).strip()
        if not path.startswith("/"):
            path = "/" + path
        if not self.path.strip():
            # Only for the *default* path: "https://host/v1" + "/v1/messages" -> ".../v1/messages"
            for prefix in ("/v1", "/v1beta/openai", "/openai"):
                if base.endswith(prefix) and path.startswith("/v1/"):
                    path = path[len("/v1") :]
                    break
        return base + path

    @property
    def key_account(self) -> str:
        """The keychain account an API key for this endpoint is stored under: one key per
        host, so a key saved for one endpoint is never sent to a different one."""
        parts = urlsplit(self.base_url.strip())
        host = (parts.hostname or "").lower()
        if parts.port:
            host = f"{host}:{parts.port}"
        return f"llm:{host}" if host else ""


class PrivacyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    store_pdf_copies: bool = True
    store_extract_cache: bool = False  # debugging only
    #: Keep uploaded tax PDFs (they hold full SSNs). config.toml-only; not in the Settings API.
    store_tax_pdf_copies: bool = False
    pii_terms: tuple[str, ...] = ()  # A11


class IngestConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    always_confirm_extractor: bool = False  # A22
    #: "auto": propose the best-scoring built-in parser/layout spec. "llm": propose the LLM
    #: statement parser for every new upload (only when an LLM is configured).
    default_extractor: Literal["auto", "llm"] = "auto"
    #: Opt-in: send the redacted pages of a tax file that the built-in parsers could not read to
    #: the configured LLM (only effective while the LLM is enabled and configured). Off by default:
    #: tax forms never leave the machine unless this is switched on.
    tax_llm_fallback: bool = False
    date_format_hints: tuple[str, ...] = ("%m/%d/%Y", "%m/%d", "%d %b %Y", "%Y-%m-%d")
    default_currency: str = "USD"


class TaxConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: Opt-in: once per calendar year, fetch the federal tax parameter file from the project's
    #: GitHub repository (a plain GET with no user data; `api/services/tax_params_update.py`).
    #: Off by default: the app makes no network request unless this or an LLM is switched on.
    auto_update_params: bool = False


class ServerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    port: int = 8756


class Settings(BaseModel):
    """The full set of non-secret settings, mirroring `config.toml` exactly."""

    model_config = ConfigDict(frozen=True)

    llm: LLMConfig = LLMConfig()
    privacy: PrivacyConfig = PrivacyConfig()
    ingest: IngestConfig = IngestConfig()
    tax: TaxConfig = TaxConfig()
    server: ServerConfig = ServerConfig()


def without_stale_tax_consent(settings: Settings) -> Settings:
    """`settings` with the tax LLM opt-in cleared when the LLM is disabled.

    The consent was given for an LLM the user was using; switching the LLM off must not leave a
    flag behind that silently resumes sending tax pages when it is switched on again.
    """
    if settings.llm.enabled or not settings.ingest.tax_llm_fallback:
        return settings
    return settings.model_copy(
        update={"ingest": settings.ingest.model_copy(update={"tax_llm_fallback": False})}
    )


def load_settings(path: Path | None = None) -> Settings:
    """Load settings: defaults -> `config.toml` (if present) -> environment variable overrides.

    Env var overrides use the pattern ``PFA_<SECTION>__<FIELD>`` (double underscore),
    e.g. ``PFA_LLM__ENABLED=true``. This lets CI and power users override settings
    without writing a file.
    """
    toml_path = path if path is not None else config_path()
    data: dict[str, object] = {}
    if toml_path.exists():
        with toml_path.open("rb") as f:
            data = tomllib.load(f)

    settings = Settings.model_validate(data)
    return _apply_env_overrides(settings)


def _apply_env_overrides(settings: Settings) -> Settings:
    prefix = "PFA_"
    overrides: dict[str, dict[str, str]] = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix) or "__" not in key:
            continue
        section, _, field_name = key[len(prefix) :].partition("__")
        section = section.lower()
        field_name = field_name.lower()
        if section not in ("llm", "privacy", "ingest", "tax", "server"):
            continue
        overrides.setdefault(section, {})[field_name] = value

    if not overrides:
        return settings

    merged = settings.model_dump()
    for section, fields in overrides.items():
        for field_name, raw_value in fields.items():
            if section == "llm" and field_name == "mode":  # legacy: none|local|remote
                merged["llm"]["enabled"] = raw_value.strip().lower() != "none"
                continue
            if field_name not in merged[section]:
                continue
            current = merged[section][field_name]
            merged[section][field_name] = _coerce(raw_value, current)
    return Settings.model_validate(merged)


def _coerce(raw: str, current: object) -> object:
    if isinstance(current, dict):
        return json.loads(raw)
    if isinstance(current, (list, tuple)):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if current is None:
        return raw if raw.strip() else None
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    return raw


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Atomically write ``settings`` to `config.toml` (A4): serialize to a temp file in the same
    directory, then `os.replace` over the destination. Never writes a secret."""
    toml_path = path if path is not None else config_path()
    ensure_home()
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _drop_none(settings.model_dump(mode="json"))
    tmp_path = toml_path.with_suffix(toml_path.suffix + ".tmp")
    with tmp_path.open("wb") as f:
        tomli_w.dump(payload, f)
    _replace_with_retry(tmp_path, toml_path)


def _replace_with_retry(src: Path, dest: Path, attempts: int = 10) -> None:
    """`os.replace`, retried briefly on `PermissionError`. config.toml is re-read on every
    request, and on Windows replacing a file another thread or process has open for reading
    fails until that (very short) read finishes. POSIX never raises here."""
    for attempt in range(attempts):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def _drop_none(value: Any) -> Any:
    """TOML has no null: omit `None` fields (they load back as their `None` default)."""
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    return value


#: Env var that supplies the LLM API key without touching the keychain (CI, containers).
LLM_API_KEY_ENV = "PFA_LLM_API_KEY"


def get_api_key(account: str) -> str | None:
    """Return the API key stored under keychain ``account``: checks the
    ``{ACCOUNT}_API_KEY`` env var first (for a plain name like ``openai``), then the OS
    keychain (service ``personal-finance-analyzer``). Never logs the value. A keychain backend that is
    unavailable (no Secret Service on a headless Linux box) reads as "no key"."""
    if account.replace("_", "").replace("-", "").isalnum():
        env_value = os.environ.get(f"{account.upper().replace('-', '_')}_API_KEY")
        if env_value:
            return env_value
    try:
        with KEYRING_LOCK:
            return keyring.get_password(_KEYRING_SERVICE, account)
    except keyring.errors.KeyringError:
        return None


def get_llm_api_key(cfg: LLMConfig) -> str | None:
    """The API key for ``cfg``'s endpoint: `PFA_LLM_API_KEY`, else the key saved in
    the OS keychain for ``cfg.base_url``'s host (see `LLMConfig.key_account`)."""
    env_value = os.environ.get(LLM_API_KEY_ENV)
    if env_value:
        return env_value
    return get_api_key(cfg.key_account) if cfg.key_account else None


def set_api_key(provider: str, key: str) -> None:
    """Store ``key`` for ``provider`` in the OS keychain. Never written to `config.toml` or the
    database."""
    with KEYRING_LOCK:
        keyring.set_password(_KEYRING_SERVICE, provider, key)


def delete_api_key(provider: str) -> None:
    """Remove ``provider``'s key from the OS keychain, if present."""
    with KEYRING_LOCK, contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(_KEYRING_SERVICE, provider)
