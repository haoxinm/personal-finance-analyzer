"""`/api/settings`, `/api/settings/test-llm`, and `/api/settings/api-key` (§3.12, A4, I7).

`GET /api/settings` never returns key material: it reports `has_key` (whether a key is present in
the Keychain or an env var override) instead of the key itself. `PUT /api/settings` rewrites
`config.toml` atomically (A4) via `personal_finance_analyzer.config.save_settings`; it never writes a secret
there either. Setting or clearing the key itself goes through the dedicated
`PUT`/`DELETE /api/settings/api-key` routes below, which write straight to the OS keychain (I7),
bound to the endpoint's host, and never echo the key back, log it, or persist it anywhere else.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Response

from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SettingsDep
from personal_finance_analyzer.config import (
    IngestConfig,
    LLMConfig,
    PrivacyConfig,
    SecretsConfig,
    ServerConfig,
    Settings,
    TaxConfig,
    delete_api_key,
    get_llm_api_key,
    save_settings,
    secret_storage_status,
    set_api_key,
    without_stale_tax_consent,
)
from personal_finance_analyzer.core.pii import compile_redactor
from personal_finance_analyzer.llm.diagnostics import check_connection
from personal_finance_analyzer.secret_store import SecretStorageError

router = APIRouter(tags=["settings"])


def _has_key(cfg: LLMConfig) -> bool:
    try:
        return get_llm_api_key(cfg) is not None
    except Exception:
        return False


def _llm_schema(cfg: LLMConfig) -> schemas.LlmSettings:
    return schemas.LlmSettings(
        enabled=cfg.enabled,
        base_url=cfg.base_url,
        api_format=cfg.api_format,
        path=cfg.path,
        model=cfg.model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        reasoning_effort=cfg.reasoning_effort,
        extra_body=dict(cfg.extra_body),
        extra_headers=dict(cfg.extra_headers),
        structured_output=cfg.structured_output,
        batch_size=cfg.batch_size,
        timeout_s=cfg.timeout_s,
        confidence_threshold=cfg.confidence_threshold,
        parse_pages_per_request=cfg.parse_pages_per_request,
        mode=cfg.mode,
        resolved_url=cfg.resolved_url if cfg.base_url.strip() else "",
        has_key=_has_key(cfg),
    )


def _llm_config(body: schemas.LlmSettings) -> LLMConfig:
    """Draft -> `LLMConfig`; the derived read-only fields are ignored."""
    return LLMConfig(
        enabled=body.enabled,
        base_url=body.base_url.strip(),
        api_format=body.api_format,
        path=body.path.strip(),
        model=body.model.strip(),
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        reasoning_effort=body.reasoning_effort,
        extra_body=dict(body.extra_body),
        extra_headers=dict(body.extra_headers),
        structured_output=body.structured_output,
        batch_size=body.batch_size,
        timeout_s=body.timeout_s,
        confidence_threshold=body.confidence_threshold,
        parse_pages_per_request=body.parse_pages_per_request,
    )


def _settings_schema(settings: Settings) -> schemas.Settings:
    return schemas.Settings(
        llm=_llm_schema(settings.llm),
        privacy=schemas.PrivacySettings(
            store_pdf_copies=settings.privacy.store_pdf_copies,
            store_extract_cache=settings.privacy.store_extract_cache,
            pii_terms=list(settings.privacy.pii_terms),
        ),
        ingest=schemas.IngestSettings(
            always_confirm_extractor=settings.ingest.always_confirm_extractor,
            default_extractor=settings.ingest.default_extractor,
            tax_llm_fallback=settings.ingest.tax_llm_fallback,
            date_format_hints=list(settings.ingest.date_format_hints),
            default_currency=settings.ingest.default_currency,
        ),
        server=schemas.ServerSettings(port=settings.server.port),
        tax=schemas.TaxSettings(auto_update_params=settings.tax.auto_update_params),
    )


@router.get("/settings", response_model=schemas.Settings)
def get_settings_route(settings: SettingsDep) -> schemas.Settings:
    return _settings_schema(settings)


@router.put("/settings", response_model=schemas.Settings)
def put_settings(body: schemas.Settings, current: SettingsDep) -> schemas.Settings:
    new_settings = Settings(
        llm=_llm_config(body.llm),
        privacy=PrivacyConfig(
            store_pdf_copies=body.privacy.store_pdf_copies,
            store_extract_cache=body.privacy.store_extract_cache,
            # config.toml-only: the Settings API must not reset it.
            store_tax_pdf_copies=current.privacy.store_tax_pdf_copies,
            pii_terms=tuple(body.privacy.pii_terms),
        ),
        ingest=IngestConfig(
            always_confirm_extractor=body.ingest.always_confirm_extractor,
            default_extractor=body.ingest.default_extractor,
            tax_llm_fallback=body.ingest.tax_llm_fallback,
            date_format_hints=tuple(body.ingest.date_format_hints),
            default_currency=body.ingest.default_currency,
        ),
        server=ServerConfig(port=body.server.port),
        tax=(
            TaxConfig(auto_update_params=body.tax.auto_update_params)
            if "tax" in body.model_fields_set
            else current.tax
        ),
        # Changed only through `PUT /settings/secret-storage`, which carries the warning.
        secrets=current.secrets,
    )
    new_settings = without_stale_tax_consent(new_settings)
    save_settings(new_settings)
    return _settings_schema(new_settings)


def _key_account(base_url: str, saved: LLMConfig) -> str:
    account = LLMConfig(base_url=base_url or saved.base_url).key_account
    if not account:
        raise HTTPException(
            status_code=400,
            detail="No LLM address (base URL) to attach the key to. Set the address first, "
            "or pass base_url.",
        )
    return account


@router.put("/settings/api-key", status_code=204, response_class=Response)
def put_api_key(body: schemas.ApiKeyPut, settings: SettingsDep) -> Response:
    """Store `body.api_key` for the endpoint's host in the OS keychain (I7), or, only when there is
    no keychain and the user enabled `[secrets] allow_file_store`, in the 0600 file store. Never
    returned, logged, or written to `config.toml`/the database. `503` (with an actionable message)
    when no backend can take it."""
    try:
        set_api_key(_key_account(body.base_url, settings.llm), body.api_key)
    except SecretStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return Response(status_code=204)


@router.delete("/settings/api-key", status_code=204, response_class=Response)
def delete_api_key_route(settings: SettingsDep, base_url: str = "") -> Response:
    """Remove the key stored for the endpoint's host, if present (I7). Idempotent."""
    try:
        delete_api_key(_key_account(base_url, settings.llm))
    except SecretStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return Response(status_code=204)


def _secret_storage_schema(llm: LLMConfig) -> schemas.SecretStorageStatus:
    status = secret_storage_status(llm)
    return schemas.SecretStorageStatus(
        key_source=status.key_source,
        storage=status.storage,
        keychain_name=status.keychain_name,
        keychain_usable=status.keychain_usable,
        keychain_detail=status.keychain_detail,
        env_var=status.env_var,
        file_store_enabled=status.file_store_enabled,
        file_store_path=status.file_store_path,
        file_store_has_keys=status.file_store_has_keys,
        guidance=status.guidance,
        warning=status.warning,
    )


@router.get("/settings/secret-storage", response_model=schemas.SecretStorageStatus)
def get_secret_storage(settings: SettingsDep, base_url: str = "") -> schemas.SecretStorageStatus:
    """Which backend holds the LLM API key (environment variable / keychain name / plaintext file
    / none), with platform-specific setup advice when no keychain works. Read-only; never returns
    a key. `base_url` (default: the saved one) picks the endpoint whose key is looked up."""
    llm = settings.llm.model_copy(update={"base_url": base_url.strip() or settings.llm.base_url})
    return _secret_storage_schema(llm)


@router.put("/settings/secret-storage", response_model=schemas.SecretStorageStatus)
def put_secret_storage(
    body: schemas.SecretStoragePut, settings: SettingsDep
) -> schemas.SecretStorageStatus:
    """Switch the plaintext file store on or off (`[secrets] allow_file_store`; only used when the
    OS keychain is unavailable)."""
    save_settings(
        settings.model_copy(
            update={"secrets": SecretsConfig(allow_file_store=body.allow_file_store)}
        )
    )
    return _secret_storage_schema(settings.llm)


@router.post("/settings/pii-test", response_model=schemas.PiiTestResponse)
def pii_test(body: schemas.PiiTestRequest, settings: SettingsDep) -> schemas.PiiTestResponse:
    """Apply personal-info entries (the unsaved draft in `body.terms`, else the saved
    `[privacy] pii_terms`) to a pasted sample. Pure and local: nothing is sent anywhere, stored or
    logged, and the entries are never echoed back."""
    terms = settings.privacy.pii_terms if body.terms is None else body.terms
    redacted, count = compile_redactor(terms).redact(body.text)
    return schemas.PiiTestResponse(redacted_text=redacted, count=count)


@router.post("/settings/test-llm", response_model=schemas.TestLlmResponse)
def test_llm_settings(
    settings: SettingsDep,
    body: Annotated[schemas.TestLlmRequest | None, Body()] = None,
) -> schemas.TestLlmResponse:
    """One-row round trip via the same `classify_batch` egress path every real classification
    uses. Tests the unsaved draft in `body.llm` when given, else the saved settings."""
    cfg = _llm_config(body.llm) if body is not None and body.llm is not None else settings.llm
    result = check_connection(cfg)
    return schemas.TestLlmResponse(
        ok=result.ok,
        detail=result.detail,
        mode=cfg.mode,
        resolved_url=cfg.resolved_url if cfg.base_url.strip() else "",
        latency_ms=result.latency_ms,
    )
