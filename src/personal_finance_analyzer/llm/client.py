"""A small, dependency-light LLM client speaking three wire formats over plain HTTPS/HTTP.

- ``openai_chat`` — ``POST {base_url}/v1/chat/completions`` (OpenAI, Ollama, LM Studio, vLLM,
  llama.cpp server, OpenRouter, Gemini's OpenAI-compatible endpoint, ...)
- ``openai_responses`` — ``POST {base_url}/v1/responses`` (OpenAI Responses API)
- ``anthropic_messages`` — ``POST {base_url}/v1/messages`` (Anthropic Messages API)

The path is configurable (`LLMConfig.path`), so any server that speaks one of these formats at a
different route works too. This is the **only** module that imports an HTTP client (I2); it is
called only from the egress seams (see `personal_finance_analyzer/llm/__init__.py`).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from personal_finance_analyzer.config import LLMConfig, get_llm_api_key
from personal_finance_analyzer.core.errors import LLMError

#: `anthropic-version` header sent to Anthropic Messages endpoints.
ANTHROPIC_VERSION = "2023-06-01"

#: Anthropic requires `max_tokens`; used when `[llm] max_tokens` is unset.
_ANTHROPIC_DEFAULT_MAX_TOKENS = 8192

#: Status codes that mean "the server didn't like the structured-output request field" closely
#: enough to retry once without it (many local servers don't implement JSON-schema output).
_SCHEMA_REJECTED_STATUSES = frozenset({400, 404, 415, 422, 501})


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int
    #: "json_schema" when the server accepted the schema; "text_fallback" when it was retried
    #: without one (or none was requested).
    schema_mode: str


def complete(
    cfg: LLMConfig,
    *,
    system: str,
    user: str,
    json_schema: dict[str, Any] | None = None,
    schema_name: str = "result",
) -> LLMResponse:
    """Send one system + user prompt to ``cfg``'s endpoint and return the reply text.

    Raises:
        LLMError: not configured, network failure, non-2xx response, or an unreadable reply.
    """
    if not cfg.is_configured:
        raise LLMError(
            "no LLM configured (enable it and set base_url: "
            "`pfa llm set ...` or the Settings screen)"
        )

    use_schema = json_schema is not None and cfg.structured_output
    started = time.monotonic()
    try:
        status, body = _post(
            cfg, _build_body(cfg, system, user, json_schema if use_schema else None, schema_name)
        )
        schema_mode = "json_schema" if use_schema else "text_fallback"
        if use_schema and status in _SCHEMA_REJECTED_STATUSES:
            status, body = _post(cfg, _build_body(cfg, system, user, None, schema_name))
            schema_mode = "text_fallback"
    except httpx.TimeoutException as exc:
        raise LLMError(
            f"LLM request to {cfg.resolved_url} failed: timed out after {cfg.timeout_s}s"
        ) from exc
    except (httpx.HTTPError, OSError) as exc:
        raise LLMError(f"LLM request to {cfg.resolved_url} failed: {exc}") from exc
    latency_ms = int((time.monotonic() - started) * 1000)

    if status >= 400:
        hint = (
            " (no model is set — this server may need one; set Model in Settings "
            "or `pfa llm set --model ...`)"
            if not cfg.model.strip()
            else ""
        )
        raise LLMError(
            f"LLM endpoint {cfg.resolved_url} returned HTTP {status}: {_error_detail(body)}{hint}"
        )
    if not isinstance(body, dict):
        raise LLMError(f"LLM endpoint returned a non-JSON response: {str(body)[:300]}")

    text, tokens_in, tokens_out = _parse_response(cfg.api_format, body)
    return LLMResponse(
        text=text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        schema_mode=schema_mode,
    )


def build_request_preview(
    cfg: LLMConfig, *, system: str, user: str, json_schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The exact JSON body `complete` would send first (no network call, no headers/key)."""
    use_schema = json_schema is not None and cfg.structured_output
    return _build_body(cfg, system, user, json_schema if use_schema else None, "result")


# ------------------------------------------------------------------------------------------------
# Request building
# ------------------------------------------------------------------------------------------------


def _build_body(
    cfg: LLMConfig,
    system: str,
    user: str,
    json_schema: dict[str, Any] | None,
    schema_name: str,
) -> dict[str, Any]:
    if cfg.api_format == "anthropic_messages":
        body = _anthropic_body(cfg, system, user, json_schema)
    elif cfg.api_format == "openai_responses":
        body = _responses_body(cfg, system, user, json_schema, schema_name)
    else:
        body = _chat_body(cfg, system, user, json_schema, schema_name)
    body.update(cfg.extra_body)
    return body


def _model_field(cfg: LLMConfig) -> dict[str, str]:
    """`{"model": ...}`, or nothing when blank (single-model local servers don't need it)."""
    model = cfg.model.strip()
    return {"model": model} if model else {}


def _chat_body(
    cfg: LLMConfig, system: str, user: str, json_schema: dict[str, Any] | None, schema_name: str
) -> dict[str, Any]:
    body: dict[str, Any] = {
        **_model_field(cfg),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
    }
    if cfg.temperature is not None:
        body["temperature"] = cfg.temperature
    if cfg.max_tokens is not None:
        body["max_tokens"] = cfg.max_tokens
    if cfg.reasoning_effort:
        body["reasoning_effort"] = cfg.reasoning_effort
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": json_schema, "strict": False},
        }
    return body


def _responses_body(
    cfg: LLMConfig, system: str, user: str, json_schema: dict[str, Any] | None, schema_name: str
) -> dict[str, Any]:
    body: dict[str, Any] = {
        **_model_field(cfg),
        "instructions": system,
        "input": [{"role": "user", "content": user}],
        "store": False,
    }
    if cfg.temperature is not None:
        body["temperature"] = cfg.temperature
    if cfg.max_tokens is not None:
        body["max_output_tokens"] = cfg.max_tokens
    if cfg.reasoning_effort:
        body["reasoning"] = {"effort": cfg.reasoning_effort}
    if json_schema is not None:
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": json_schema,
                "strict": False,
            }
        }
    return body


def _anthropic_body(
    cfg: LLMConfig, system: str, user: str, json_schema: dict[str, Any] | None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        **_model_field(cfg),
        "max_tokens": cfg.max_tokens or _ANTHROPIC_DEFAULT_MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if cfg.temperature is not None:
        body["temperature"] = cfg.temperature
    output_config: dict[str, Any] = {}
    if cfg.reasoning_effort:
        output_config["effort"] = cfg.reasoning_effort
    if json_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": json_schema}
    if output_config:
        body["output_config"] = output_config
    return body


def _headers(cfg: LLMConfig) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = get_llm_api_key(cfg)
    if cfg.api_format == "anthropic_messages":
        headers["anthropic-version"] = ANTHROPIC_VERSION
        if api_key:
            headers["x-api-key"] = api_key
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(cfg.extra_headers)
    return headers


def _post(cfg: LLMConfig, body: dict[str, Any]) -> tuple[int, Any]:
    """POST ``body``; returns ``(status, parsed JSON or raw text)``. Retries once on 429/5xx."""
    for attempt in range(2):
        with httpx.Client(timeout=cfg.timeout_s) as http:
            resp = http.post(cfg.resolved_url, headers=_headers(cfg), json=body)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
            time.sleep(1.0)
            continue
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, resp.text
    raise AssertionError("unreachable")  # pragma: no cover


# ------------------------------------------------------------------------------------------------
# Response parsing
# ------------------------------------------------------------------------------------------------


def _parse_response(api_format: str, body: dict[str, Any]) -> tuple[str, int | None, int | None]:
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    assert isinstance(usage, dict)
    try:
        if api_format == "anthropic_messages":
            if body.get("stop_reason") == "refusal":
                raise LLMError("the model declined the request (stop_reason=refusal)")
            text = "".join(
                block.get("text", "")
                for block in body.get("content", [])
                if isinstance(block, dict) and block.get("type") == "text"
            )
            return text, usage.get("input_tokens"), usage.get("output_tokens")
        if api_format == "openai_responses":
            if isinstance(body.get("output_text"), str):
                text = body["output_text"]
            else:
                text = "".join(
                    part.get("text", "")
                    for item in body.get("output", [])
                    if isinstance(item, dict) and item.get("type") == "message"
                    for part in item.get("content", [])
                    if isinstance(part, dict) and part.get("type") == "output_text"
                )
            return text, usage.get("input_tokens"), usage.get("output_tokens")
        message = body["choices"][0]["message"]
        content = message.get("content") or ""
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return str(content), usage.get("prompt_tokens"), usage.get("completion_tokens")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMError(f"malformed LLM response ({exc}): {json.dumps(body)[:300]}") from exc


def _error_detail(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            return str(err.get("message") or err)[:500]
        return str(err)[:500]
    return str(body)[:500]


def extract_json(text: str) -> Any:
    """Parse a model's JSON reply, tolerating a markdown code fence or prose around it.

    Raises:
        LLMError: no JSON value could be found.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost {...} or [...] span (a model that adds a sentence first),
    # trying whichever opener appears first in the text.
    spans = sorted(
        (stripped.find(op), op, cl) for op, cl in (("{", "}"), ("[", "]")) if op in stripped
    )
    for start, _opener, closer in spans:
        end = stripped.rfind(closer)
        if end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"LLM response was not valid JSON: {text[:300]!r}")
