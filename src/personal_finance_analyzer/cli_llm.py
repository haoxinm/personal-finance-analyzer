"""`pfa llm ...`: configure and test the LLM without editing config.toml by hand."""

from __future__ import annotations

import json
import sys
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer

from personal_finance_analyzer.config import (
    DEFAULT_PATHS,
    LLMConfig,
    SecretStorageStatus,
    Settings,
    delete_api_key,
    env_overridden_fields,
    env_override_var,
    get_llm_api_key,
    load_file_settings,
    load_settings,
    save_settings,
    secret_storage_status,
    set_api_key,
    without_stale_tax_consent,
)
from personal_finance_analyzer.core.paths import config_path
from personal_finance_analyzer.llm.diagnostics import check_connection
from personal_finance_analyzer.secret_store import SecretStorageError

llm_app = typer.Typer(
    help=(
        "Configure the LLM used for classification and statement parsing. Changes are written "
        "to config.toml and take effect immediately, even for a running `pfa serve`."
    ),
    no_args_is_help=True,
)

_FORMATS = tuple(DEFAULT_PATHS)

#: name -> (base_url, api_format)
PRESETS: dict[str, tuple[str, str]] = {
    "ollama": ("http://localhost:11434", "openai_chat"),
    "lm-studio": ("http://localhost:1234", "openai_chat"),
    "openai": ("https://api.openai.com", "openai_chat"),
    "openai-responses": ("https://api.openai.com", "openai_responses"),
    "anthropic": ("https://api.anthropic.com", "anthropic_messages"),
    "openrouter": ("https://openrouter.ai/api", "openai_chat"),
}


def _fail(message: str, code: int = 2) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code=code)


def _host(base_url: str) -> str:
    return urlsplit(base_url.strip()).netloc or base_url


def _has_key(cfg: LLMConfig) -> bool:
    try:
        return get_llm_api_key(cfg, adopt=False) is not None  # `show` never writes the keychain
    except Exception:
        return False


def describe_secret_storage(status: SecretStorageStatus) -> str:
    """One line on where the current key comes from (or would be saved): shared with `pfa doctor`."""
    if status.key_source == "env":
        return f"environment variable {status.env_var}"
    if status.key_source == "keychain":
        if status.key_legacy:
            return (
                f"OS keychain ({status.keychain_name}), found under the old name "
                "'spend-analyzer'; it will be moved on next use"
            )
        return f"OS keychain ({status.keychain_name})"
    if status.key_source == "file":
        return f"plaintext file store ({status.file_store_path})"
    if status.storage == "keychain":
        return f"none saved (a saved key would go to the OS keychain: {status.keychain_name})"
    if status.storage == "file":
        return f"none saved (a saved key would go to the plaintext file {status.file_store_path})"
    return "none (no usable keychain; see `pfa doctor`)"


def _describe(settings: Settings) -> None:
    cfg = settings.llm
    typer.echo(f"mode: {cfg.mode}")
    if cfg.base_url:
        typer.echo(f"url:  {cfg.resolved_url}")
    if not cfg.enabled:
        typer.echo("(disabled — run `pfa llm enable` to turn it on)")
    elif cfg.mode == "none":
        typer.echo("(not usable yet: a base URL is required)")
    elif cfg.mode == "remote":
        typer.echo(
            f"WARNING: merchant descriptions — and, for statements you parse with the LLM, "
            f"page text — will be sent to {_host(cfg.base_url)}."
        )
        if settings.ingest.tax_llm_fallback:
            typer.echo(
                "WARNING: tax_llm_fallback is on: pages of tax forms the built-in parsers cannot "
                f"read are sent, redacted, to {_host(cfg.base_url)}."
            )


def _parse_optional(value: str, kind: type[int] | type[float], flag: str) -> Any:
    if value.strip().lower() in ("none", ""):
        return None
    try:
        return kind(value)
    except ValueError:
        raise _fail(f"{flag} must be a number or 'none', got {value!r}") from None


@llm_app.command("show")
def show() -> None:
    """Print the current LLM settings (never the API key)."""
    settings = load_settings()
    cfg = settings.llm
    typer.echo(f"config file: {config_path()}")
    for name in LLMConfig.model_fields:
        typer.echo(f"{name}: {json.dumps(getattr(cfg, name))}")
    typer.echo(f"mode: {cfg.mode}")
    typer.echo(f"resolved_url: {cfg.resolved_url if cfg.base_url else ''}")
    typer.echo(f"api key on file: {'yes' if _has_key(cfg) else 'no'}")
    typer.echo(f"api key source: {describe_secret_storage(secret_storage_status(cfg))}")
    typer.echo(f"[ingest] default_extractor: {settings.ingest.default_extractor}")
    typer.echo(f"[ingest] tax_llm_fallback: {json.dumps(settings.ingest.tax_llm_fallback)}")


@llm_app.command("set")
def set_cmd(
    preset: Annotated[
        str | None, typer.Option("--preset", help=f"One of: {', '.join(PRESETS)}.")
    ] = None,
    base_url: Annotated[
        str | None, typer.Option("--base-url", help="e.g. http://localhost:11434")
    ] = None,
    api_format: Annotated[
        str | None,
        typer.Option("--format", help="openai_chat | openai_responses | anthropic_messages"),
    ] = None,
    path: Annotated[
        str | None, typer.Option("--path", help="Request path ('' = format default).")
    ] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    temperature: Annotated[
        str | None, typer.Option("--temperature", help="float or 'none'")
    ] = None,
    max_tokens: Annotated[str | None, typer.Option("--max-tokens", help="int or 'none'")] = None,
    reasoning_effort: Annotated[
        str | None,
        typer.Option("--reasoning-effort", help="minimal|low|medium|high|xhigh|max; '' clears"),
    ] = None,
    extra_body: Annotated[
        str | None, typer.Option("--extra-body", help="JSON object merged into every request body")
    ] = None,
    extra_header: Annotated[
        list[str] | None, typer.Option("--extra-header", help="KEY=VALUE (repeatable)")
    ] = None,
    clear_extra_headers: Annotated[bool, typer.Option("--clear-extra-headers")] = False,
    structured_output: Annotated[
        bool | None, typer.Option("--structured-output/--no-structured-output")
    ] = None,
    batch_size: Annotated[int | None, typer.Option("--batch-size", min=1)] = None,
    timeout: Annotated[int | None, typer.Option("--timeout", min=1, help="seconds")] = None,
    confidence_threshold: Annotated[float | None, typer.Option("--confidence-threshold")] = None,
    pages_per_request: Annotated[
        int | None, typer.Option("--pages-per-request", min=0, help="0 = all pages at once")
    ] = None,
    enable: Annotated[bool | None, typer.Option("--enable/--disable")] = None,
    default_extractor: Annotated[
        str | None, typer.Option("--default-extractor", help="auto | llm")
    ] = None,
    tax_llm_fallback: Annotated[
        bool | None,
        typer.Option(
            "--tax-llm-fallback/--no-tax-llm-fallback",
            help="Send redacted pages of tax forms the built-in parsers cannot read to the LLM.",
        ),
    ] = None,
) -> None:
    """Change LLM settings. Only the options you pass are changed."""
    settings = load_file_settings()  # edit what config.toml holds, never an env override
    updates: dict[str, Any] = {}

    if preset is not None:
        if preset not in PRESETS:
            raise _fail(f"unknown --preset {preset!r}; choose one of: {', '.join(PRESETS)}")
        p_url, p_format = PRESETS[preset]
        if base_url is None:
            base_url = p_url
        if api_format is None:
            api_format = p_format
    if base_url is not None:
        updates["base_url"] = base_url.strip()
    if api_format is not None:
        if api_format not in _FORMATS:
            raise _fail(f"invalid --format {api_format!r}; choose one of: {', '.join(_FORMATS)}")
        updates["api_format"] = api_format
    if path is not None:
        updates["path"] = path.strip()
    if model is not None:
        updates["model"] = model.strip()
    if temperature is not None:
        updates["temperature"] = _parse_optional(temperature, float, "--temperature")
    if max_tokens is not None:
        updates["max_tokens"] = _parse_optional(max_tokens, int, "--max-tokens")
    if reasoning_effort is not None:
        updates["reasoning_effort"] = reasoning_effort.strip()
    if extra_body is not None:
        try:
            parsed = json.loads(extra_body)
        except json.JSONDecodeError as exc:
            raise _fail(f"--extra-body must be valid JSON: {exc}") from None
        if not isinstance(parsed, dict):
            raise _fail("--extra-body must be a JSON object")
        updates["extra_body"] = parsed
    if extra_header or clear_extra_headers:
        headers = {} if clear_extra_headers else dict(settings.llm.extra_headers)
        for item in extra_header or []:
            key, sep, value = item.partition("=")
            if not sep or not key.strip():
                raise _fail(f"--extra-header must be KEY=VALUE, got {item!r}")
            headers[key.strip()] = value.strip()
        updates["extra_headers"] = headers
    if structured_output is not None:
        updates["structured_output"] = structured_output
    if batch_size is not None:
        updates["batch_size"] = batch_size
    if timeout is not None:
        updates["timeout_s"] = timeout
    if confidence_threshold is not None:
        updates["confidence_threshold"] = confidence_threshold
    if pages_per_request is not None:
        updates["parse_pages_per_request"] = pages_per_request
    if enable is not None:
        updates["enabled"] = enable

    new_ingest = settings.ingest
    if default_extractor is not None:
        if default_extractor not in ("auto", "llm"):
            raise _fail("--default-extractor must be 'auto' or 'llm'")
        new_ingest = settings.ingest.model_copy(update={"default_extractor": default_extractor})
    if tax_llm_fallback is not None:
        new_ingest = new_ingest.model_copy(update={"tax_llm_fallback": tax_llm_fallback})

    try:
        new_llm = LLMConfig.model_validate({**settings.llm.model_dump(), **updates})
    except ValueError as exc:
        raise _fail(f"invalid settings: {exc}") from None
    new_settings = settings.model_copy(update={"llm": new_llm, "ingest": new_ingest})
    if enable is False:
        new_settings = without_stale_tax_consent(new_settings)
    save_settings(new_settings)
    typer.echo(f"saved to {config_path()}")
    _warn_env_overrides()
    _describe(load_settings())


def _warn_env_overrides() -> None:
    """Say so when an environment variable still decides an LLM or ingest setting: what was just
    saved to config.toml is not what runs while the variable is set."""
    for section, name in sorted(env_overridden_fields()):
        if section in ("llm", "ingest"):
            var = env_override_var(section, name)
            typer.echo(
                f"note: {var} is set and overrides [{section}] {name}; the saved value applies "
                "once it is unset.",
                err=True,
            )


def _set_enabled(value: bool) -> None:
    settings = load_file_settings()
    new_settings = settings.model_copy(
        update={"llm": settings.llm.model_copy(update={"enabled": value})}
    )
    if not value:
        new_settings = without_stale_tax_consent(new_settings)
    save_settings(new_settings)
    typer.echo(f"LLM {'enabled' if value else 'disabled'}.")
    _warn_env_overrides()
    _describe(load_settings())


@llm_app.command("enable")
def enable_cmd() -> None:
    """Turn the LLM on (needs a base URL; see `llm set`)."""
    _set_enabled(True)


@llm_app.command("disable")
def disable_cmd() -> None:
    """Turn the LLM off. Nothing is ever sent anywhere while it is off."""
    _set_enabled(False)


def _key_account(base_url: str | None, saved: LLMConfig) -> str:
    account = LLMConfig(base_url=base_url or saved.base_url).key_account
    if not account:
        raise _fail(
            "No base URL to attach the key to. Run `llm set --base-url ...` or pass --base-url."
        )
    return account


@llm_app.command("set-key")
def set_key(
    base_url: Annotated[
        str | None, typer.Option("--base-url", help="Defaults to the saved base URL.")
    ] = None,
) -> None:
    """Store the API key for the endpoint's host in the OS keychain (prompted, or from stdin).

    With no keychain (headless Linux, WSL, SSH) it fails with setup advice, unless
    `[secrets] allow_file_store = true` opts in to a plaintext 0600 file."""
    account = _key_account(base_url, load_settings().llm)
    raw = typer.prompt("API key", hide_input=True) if sys.stdin.isatty() else sys.stdin.readline()
    key = raw.strip()
    if not key:
        raise _fail("no API key given")
    try:
        where = set_api_key(account, key)
    except SecretStorageError as exc:
        raise _fail(f"could not store the key: {exc}", 1) from None
    target = "the OS keychain" if where == "keychain" else "the plaintext file store"
    typer.echo(f"API key stored for {account.removeprefix('llm:')} in {target}.")
    if where == "file":
        typer.echo(f"WARNING: {secret_storage_status(None).warning}", err=True)


@llm_app.command("clear-key")
def clear_key(
    base_url: Annotated[
        str | None, typer.Option("--base-url", help="Defaults to the saved base URL.")
    ] = None,
) -> None:
    """Remove the stored API key for the endpoint's host."""
    account = _key_account(base_url, load_settings().llm)
    try:
        delete_api_key(account)
    except SecretStorageError as exc:
        raise _fail(f"could not clear the key: {exc}", 1) from None
    typer.echo(f"API key cleared for {account.removeprefix('llm:')}.")


@llm_app.command("test")
def test_cmd() -> None:
    """Make one real round trip to the configured LLM. Exit code 1 on failure."""
    cfg = load_settings().llm
    result = check_connection(cfg)
    if result.ok:
        typer.echo(f"OK  {result.detail}")
        return
    typer.echo(f"FAIL  {result.detail}", err=True)
    raise typer.Exit(code=1)
