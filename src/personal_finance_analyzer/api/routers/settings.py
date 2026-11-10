"""`/api/settings`, `/api/settings/test-llm`, and `/api/settings/api-key` (§3.12, A4, I7).

`GET /api/settings` never returns key material: it reports `has_key` (whether a key is present in
the Keychain or an env var override) instead of the key itself. `PUT /api/settings` rewrites
`config.toml` atomically (A4) via `personal_finance_analyzer.config.save_settings`; it never writes a secret
there either. Setting or clearing the key itself goes through the dedicated
`PUT`/`DELETE /api/settings/api-key` routes below, which write straight to the OS keychain (I7),
bound to the endpoint's host, and never echo the key back, log it, or persist it anywhere else.
"""

from __future__ import annotations

from typing import Annotated, Literal

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
    env_overridden_fields,
    env_override_var,
    get_llm_api_key,
    keep_file_values,
    load_file_settings,
    load_settings,
    move_file_keys_to_keychain,
    save_settings,
    secret_storage_status,
    set_api_key,
    without_stale_tax_consent,
)
from personal_finance_analyzer.core.pii import compile_redactor
from personal_finance_analyzer.llm.diagnostics import check_connection
from personal_finance_analyzer.secret_store import (
    SecretStorageError,
    file_store_file_exists,
    inspect_keychain,
    remove_file_store,
)

router = APIRouter(tags=["settings"])


def _has_key(cfg: LLMConfig) -> bool:
    try:
        # A GET must not write the keychain, nor read the pre-rename entry (which can make macOS
        # ask the user to allow it): a key still under the old name shows once it is used.
        return get_llm_api_key(cfg, adopt=False, probe_legacy=False) is not None
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
        env_overrides=sorted(f"{section}.{name}" for section, name in env_overridden_fields()),
    )


@router.get("/settings", response_model=schemas.Settings)
def get_settings_route(settings: SettingsDep) -> schemas.Settings:
    return _settings_schema(settings)


@router.put("/settings", response_model=schemas.Settings)
def put_settings(body: schemas.Settings) -> schemas.Settings:
    """Save the draft to `config.toml`. Settings an environment variable (`PFA_<SECTION>__<FIELD>`)
    controls are not written: the file keeps its own value for them (so a one-off override never
    becomes permanent), and the response lists them in `env_overrides` with the effective values."""
    current = load_file_settings()  # the file's values, not the env-merged ones
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
        # config.toml-only: the Settings API must not reset it.
        storage=current.storage,
        # Changed only through `PUT /settings/secret-storage`, which carries the warning.
        secrets=current.secrets,
    )
    new_settings = without_stale_tax_consent(keep_file_values(new_settings, current))
    save_settings(new_settings)
    return _settings_schema(load_settings())


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


def _secret_storage_schema(
    llm: LLMConfig,
    *,
    probe_legacy: bool = False,
    outcome: Literal["none", "moved", "deleted", "kept"] = "none",
    moved_keys: int = 0,
    kept_keys: int = 0,
) -> schemas.SecretStorageStatus:
    status = secret_storage_status(llm, probe_legacy=probe_legacy)
    return schemas.SecretStorageStatus(
        key_source=status.key_source,
        storage=status.storage,
        keychain_name=status.keychain_name,
        keychain_usable=status.keychain_usable,
        keychain_detail=status.keychain_detail,
        env_var=status.env_var,
        key_legacy=status.key_legacy,
        file_store_enabled=status.file_store_enabled,
        file_store_path=status.file_store_path,
        file_store_has_keys=status.file_store_has_keys,
        legacy_checked=status.legacy_checked,
        file_key_outcome=outcome,
        moved_keys=moved_keys,
        kept_keys=kept_keys,
        guidance=status.guidance,
        warning=status.warning,
        file_store_env_var=status.file_store_env_var,
    )


@router.get("/settings/secret-storage", response_model=schemas.SecretStorageStatus)
def get_secret_storage(
    settings: SettingsDep, base_url: str = "", check_legacy: bool = False
) -> schemas.SecretStorageStatus:
    """Which backend holds the LLM API key (environment variable / keychain name / plaintext file
    / none), with platform-specific setup advice when no keychain works. Read-only; never returns
    a key. `base_url` (default: the saved one) picks the endpoint whose key is looked up.

    The key saved under the pre-rename keychain service is only looked for with
    `check_legacy=true` (the Settings screen's explicit "check" button): reading an entry another
    build of the app saved can make macOS ask the user to allow it, which a page load must not
    trigger."""
    llm = settings.llm.model_copy(update={"base_url": base_url.strip() or settings.llm.base_url})
    return _secret_storage_schema(llm, probe_legacy=check_legacy)


def _settle_file_keys(
    action: Literal["move", "delete", "keep"] | None,
) -> tuple[Literal["none", "moved", "deleted", "kept"], int, int]:
    """Decide what happens to the plaintext key file before the opt-in is switched off. Runs
    first, so a refusal or a failure leaves the settings exactly as they were."""
    if not file_store_file_exists():
        return "none", 0, 0
    keychain = inspect_keychain()
    if action is None:
        if not keychain.usable:
            raise HTTPException(
                status_code=409,
                detail="A key is saved in the plaintext key file and there is no system keychain "
                "to move it to. Choose what to do with it: file_key_action 'delete' (overwrite "
                "and remove it; enter the key again later) or 'keep' (leave it on disk, unused).",
            )
        action = "move"
    if action == "keep":
        return "kept", 0, 0
    try:
        if action == "delete":
            remove_file_store()
            return "deleted", 0, 0
        result = move_file_keys_to_keychain()
        return "moved", result.moved, result.kept_existing
    except SecretStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.put("/settings/secret-storage", response_model=schemas.SecretStorageStatus)
def put_secret_storage(
    body: schemas.SecretStoragePut, settings: SettingsDep
) -> schemas.SecretStorageStatus:
    """Switch the plaintext file store on or off (`[secrets] allow_file_store`; only used when the
    OS keychain is unavailable). `409` while an environment variable controls the opt-in: it wins
    on every load, so the toggle could not change anything.

    Switching it off does not leave a plaintext key behind unnoticed: a key still in the file is
    moved into the OS keychain when one works, otherwise the caller must choose
    (`file_key_action`) to delete the file (overwritten, then removed) or to keep it. The response
    says which happened (`file_key_outcome`), so the UI can state where the key now lives."""
    var = env_override_var("secrets", "allow_file_store")
    if var is not None:
        raise HTTPException(
            status_code=409,
            detail=f"The plaintext key file is controlled by the environment variable {var}; "
            "unset it to change this setting here.",
        )
    outcome: Literal["none", "moved", "deleted", "kept"] = "none"
    moved = kept = 0
    if body.allow_file_store:
        if body.file_key_action is not None:
            raise HTTPException(
                status_code=400, detail="file_key_action only applies when turning the file off"
            )
    else:
        outcome, moved, kept = _settle_file_keys(body.file_key_action)
    saved = load_file_settings()  # never persist other env-overridden values
    save_settings(
        saved.model_copy(update={"secrets": SecretsConfig(allow_file_store=body.allow_file_store)})
    )
    return _secret_storage_schema(settings.llm, outcome=outcome, moved_keys=moved, kept_keys=kept)


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
