"""Tests for `personal_finance_analyzer.llm.client`: per-format request building, response parsing, errors.
Never opens a socket: `client._post` is faked (or an `httpx.MockTransport` is used)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from personal_finance_analyzer.config import LLMConfig
from personal_finance_analyzer.core.errors import LLMError
from personal_finance_analyzer.llm import client
from tests.llm_fakes import FakeLLM, install, remote_cfg, wrap_text

FORMATS = ["openai_chat", "openai_responses", "anthropic_messages"]
SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_LLM_API_KEY", "sk-test-123")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    return install(monkeypatch)


def _run(fake: FakeLLM, cfg: LLMConfig, **kwargs: Any) -> client.LLMResponse:
    fake.default = fake.default or "{}"
    return client.complete(cfg, system="SYS", user="USR", **kwargs)


# ---- URLs ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "suffix"),
    [
        ("openai_chat", "/v1/chat/completions"),
        ("openai_responses", "/v1/responses"),
        ("anthropic_messages", "/v1/messages"),
    ],
)
def test_default_paths(fake: FakeLLM, fmt: str, suffix: str) -> None:
    _run(fake, remote_cfg(api_format=fmt))
    assert fake.urls == ["https://api.example.com" + suffix]


def test_custom_path(fake: FakeLLM) -> None:
    _run(fake, remote_cfg(path="/custom/chat"))
    assert fake.urls == ["https://api.example.com/custom/chat"]
    _run(fake, remote_cfg(path="no/leading/slash"))
    assert fake.urls[-1] == "https://api.example.com/no/leading/slash"


@pytest.mark.parametrize("base", ["https://api.example.com/v1", "https://api.example.com/v1/"])
def test_base_url_ending_in_v1_not_doubled(fake: FakeLLM, base: str) -> None:
    _run(fake, remote_cfg(base_url=base))
    assert fake.urls == ["https://api.example.com/v1/chat/completions"]


def test_trailing_slash_base_url(fake: FakeLLM) -> None:
    _run(fake, remote_cfg(base_url="https://api.example.com/"))
    assert fake.urls == ["https://api.example.com/v1/chat/completions"]


# ---- headers ------------------------------------------------------------------------------


def test_openai_bearer_header(fake: FakeLLM) -> None:
    _run(fake, remote_cfg())
    h = fake.headers[0]
    assert h["Authorization"] == "Bearer sk-test-123"
    assert "x-api-key" not in h
    assert "anthropic-version" not in h


def test_anthropic_headers(fake: FakeLLM) -> None:
    _run(fake, remote_cfg(api_format="anthropic_messages"))
    h = fake.headers[0]
    assert h["x-api-key"] == "sk-test-123"
    assert h["anthropic-version"] == client.ANTHROPIC_VERSION
    assert "Authorization" not in h


def test_no_key_sends_no_auth_header(fake: FakeLLM, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PFA_LLM_API_KEY")
    monkeypatch.setattr("personal_finance_analyzer.config.keyring.get_password", lambda *a: None)
    _run(fake, remote_cfg(base_url="http://localhost:11434"))
    assert "Authorization" not in fake.headers[0]


def test_extra_headers(fake: FakeLLM) -> None:
    _run(fake, remote_cfg(extra_headers={"HTTP-Referer": "https://x.example", "X-Foo": "1"}))
    assert fake.headers[0]["HTTP-Referer"] == "https://x.example"
    assert fake.headers[0]["X-Foo"] == "1"


# ---- body ---------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", FORMATS)
def test_temperature_omitted_when_none_and_sent_when_set(fake: FakeLLM, fmt: str) -> None:
    _run(fake, remote_cfg(api_format=fmt))
    _run(fake, remote_cfg(api_format=fmt, temperature=0.0))
    _run(fake, remote_cfg(api_format=fmt, temperature=0.3))
    assert "temperature" not in fake.requests[0]
    assert fake.requests[1]["temperature"] == 0.0
    assert fake.requests[2]["temperature"] == 0.3


@pytest.mark.parametrize("fmt", FORMATS)
def test_model_key_omitted_when_blank(fake: FakeLLM, fmt: str) -> None:
    _run(fake, remote_cfg(api_format=fmt, model=""), json_schema=SCHEMA)
    _run(fake, remote_cfg(api_format=fmt, model="  "))
    _run(fake, remote_cfg(api_format=fmt, model="m"))
    assert "model" not in fake.requests[0]
    assert "model" not in fake.requests[1]
    assert fake.requests[2]["model"] == "m"
    assert "model" not in client.build_request_preview(
        remote_cfg(api_format=fmt, model=""), system="S", user="U"
    )


def test_4xx_with_blank_model_adds_hint(fake: FakeLLM) -> None:
    fake.queue((400, {"error": {"message": "model required"}}))
    with pytest.raises(LLMError, match=r"400.*model required.*no model is set"):
        _run(fake, remote_cfg(model=""))


def test_4xx_with_model_has_no_hint(fake: FakeLLM) -> None:
    fake.queue((404, {"error": {"message": "unknown model"}}))
    with pytest.raises(LLMError) as exc:
        _run(fake, remote_cfg())
    assert "no model is set" not in str(exc.value)


def test_chat_body_shape(fake: FakeLLM) -> None:
    _run(fake, remote_cfg(), json_schema=SCHEMA, schema_name="thing")
    body = fake.requests[0]
    assert body["model"] == "test-model"
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USR"},
    ]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "thing"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA


def test_responses_body_shape(fake: FakeLLM) -> None:
    _run(fake, remote_cfg(api_format="openai_responses"), json_schema=SCHEMA, schema_name="thing")
    body = fake.requests[0]
    assert body["instructions"] == "SYS"
    assert body["input"] == [{"role": "user", "content": "USR"}]
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["name"] == "thing"
    assert body["text"]["format"]["schema"] == SCHEMA
    assert "messages" not in body


def test_anthropic_body_shape(fake: FakeLLM) -> None:
    _run(fake, remote_cfg(api_format="anthropic_messages"), json_schema=SCHEMA)
    body = fake.requests[0]
    assert body["system"] == "SYS"
    assert body["messages"] == [{"role": "user", "content": "USR"}]
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}


@pytest.mark.parametrize("fmt", FORMATS)
def test_no_schema_means_no_structured_fields(fake: FakeLLM, fmt: str) -> None:
    _run(fake, remote_cfg(api_format=fmt))
    body = fake.requests[0]
    assert "response_format" not in body
    assert "text" not in body
    assert "output_config" not in body


@pytest.mark.parametrize("fmt", FORMATS)
def test_structured_output_false_suppresses_schema(fake: FakeLLM, fmt: str) -> None:
    _run(fake, remote_cfg(api_format=fmt, structured_output=False), json_schema=SCHEMA)
    body = fake.requests[0]
    assert "response_format" not in body
    assert "text" not in body
    assert "output_config" not in body


def test_reasoning_effort_mapping(fake: FakeLLM) -> None:
    _run(fake, remote_cfg(reasoning_effort="high"))
    _run(fake, remote_cfg(api_format="openai_responses", reasoning_effort="low"))
    _run(fake, remote_cfg(api_format="anthropic_messages", reasoning_effort="medium"))
    assert fake.requests[0]["reasoning_effort"] == "high"
    assert fake.requests[1]["reasoning"] == {"effort": "low"}
    assert fake.requests[2]["output_config"]["effort"] == "medium"


def test_reasoning_effort_combines_with_schema_on_anthropic(fake: FakeLLM) -> None:
    cfg = remote_cfg(api_format="anthropic_messages", reasoning_effort="low")
    _run(fake, cfg, json_schema=SCHEMA)
    oc = fake.requests[0]["output_config"]
    assert oc["effort"] == "low"
    assert oc["format"]["schema"] == SCHEMA


@pytest.mark.parametrize("fmt", FORMATS)
def test_no_reasoning_effort_by_default(fake: FakeLLM, fmt: str) -> None:
    _run(fake, remote_cfg(api_format=fmt))
    body = fake.requests[0]
    assert "reasoning_effort" not in body
    assert "reasoning" not in body
    assert "output_config" not in body


def test_max_tokens_mapping(fake: FakeLLM) -> None:
    _run(fake, remote_cfg())
    _run(fake, remote_cfg(max_tokens=500))
    _run(fake, remote_cfg(api_format="openai_responses", max_tokens=500))
    _run(fake, remote_cfg(api_format="anthropic_messages"))
    _run(fake, remote_cfg(api_format="anthropic_messages", max_tokens=500))
    assert "max_tokens" not in fake.requests[0]
    assert fake.requests[1]["max_tokens"] == 500
    assert fake.requests[2]["max_output_tokens"] == 500
    assert "max_tokens" not in fake.requests[2]
    assert fake.requests[3]["max_tokens"] == 8192
    assert fake.requests[4]["max_tokens"] == 500


@pytest.mark.parametrize("fmt", FORMATS)
def test_extra_body_overrides_and_extends(fake: FakeLLM, fmt: str) -> None:
    cfg = remote_cfg(
        api_format=fmt,
        temperature=0.5,
        extra_body={"temperature": 1.0, "top_p": 0.9, "chat_template_kwargs": {"a": 1}},
    )
    _run(fake, cfg)
    body = fake.requests[0]
    assert body["temperature"] == 1.0
    assert body["top_p"] == 0.9
    assert body["chat_template_kwargs"] == {"a": 1}


def test_build_request_preview_matches_first_request(fake: FakeLLM) -> None:
    cfg = remote_cfg(temperature=0.2)
    preview = client.build_request_preview(cfg, system="SYS", user="USR", json_schema=SCHEMA)
    _run(fake, cfg, json_schema=SCHEMA, schema_name="result")
    assert preview == fake.requests[0]
    assert not any("sk-test" in str(v) for v in preview.values())


def test_preview_without_schema_when_structured_output_off() -> None:
    cfg = remote_cfg(structured_output=False)
    preview = client.build_request_preview(cfg, system="S", user="U", json_schema=SCHEMA)
    assert "response_format" not in preview


# ---- responses ----------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", FORMATS)
def test_parses_text_and_tokens(fake: FakeLLM, fmt: str) -> None:
    fake.queue((200, wrap_text(fmt, '{"a": 1}', tokens_in=11, tokens_out=22)))
    resp = client.complete(remote_cfg(api_format=fmt), system="s", user="u")
    assert resp.text == '{"a": 1}'
    assert resp.tokens_in == 11
    assert resp.tokens_out == 22
    assert resp.latency_ms >= 0
    assert resp.schema_mode == "text_fallback"  # no schema requested


@pytest.mark.parametrize("fmt", FORMATS)
def test_schema_mode_reported(fake: FakeLLM, fmt: str) -> None:
    fake.queue("{}")
    resp = client.complete(remote_cfg(api_format=fmt), system="s", user="u", json_schema=SCHEMA)
    assert resp.schema_mode == "json_schema"


def test_missing_usage_gives_none_tokens(fake: FakeLLM) -> None:
    fake.queue((200, {"choices": [{"message": {"content": "hi"}}]}))
    resp = client.complete(remote_cfg(), system="s", user="u")
    assert resp.text == "hi"
    assert resp.tokens_in is None
    assert resp.tokens_out is None


def test_chat_content_parts_are_joined(fake: FakeLLM) -> None:
    body = {"choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"text": "b"}]}}]}
    fake.queue((200, body))
    assert client.complete(remote_cfg(), system="s", user="u").text == "ab"


def test_responses_output_text_shortcut(fake: FakeLLM) -> None:
    fake.queue((200, {"output_text": "direct", "usage": {}}))
    resp = client.complete(remote_cfg(api_format="openai_responses"), system="s", user="u")
    assert resp.text == "direct"


def test_responses_skips_reasoning_items(fake: FakeLLM) -> None:
    body = {
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": "answer"}]},
        ]
    }
    fake.queue((200, body))
    resp = client.complete(remote_cfg(api_format="openai_responses"), system="s", user="u")
    assert resp.text == "answer"


def test_anthropic_skips_thinking_blocks(fake: FakeLLM) -> None:
    body = {
        "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "answer"},
        ],
        "stop_reason": "end_turn",
    }
    fake.queue((200, body))
    resp = client.complete(remote_cfg(api_format="anthropic_messages"), system="s", user="u")
    assert resp.text == "answer"


def test_anthropic_refusal_raises(fake: FakeLLM) -> None:
    fake.queue((200, {"content": [], "stop_reason": "refusal", "usage": {}}))
    with pytest.raises(LLMError, match="refusal"):
        client.complete(remote_cfg(api_format="anthropic_messages"), system="s", user="u")


@pytest.mark.parametrize("fmt", FORMATS)
def test_malformed_response_raises(fake: FakeLLM, fmt: str) -> None:
    fake.queue((200, {"unexpected": True}))
    if fmt == "openai_chat":
        with pytest.raises(LLMError, match="malformed"):
            client.complete(remote_cfg(api_format=fmt), system="s", user="u")
    else:
        # No text found: returns an empty reply rather than crashing; callers fail on JSON parse.
        assert client.complete(remote_cfg(api_format=fmt), system="s", user="u").text == ""


# ---- errors / retries ---------------------------------------------------------------------


def test_http_401_raises_with_message(fake: FakeLLM) -> None:
    fake.queue((401, {"error": {"message": "invalid x-api-key"}}))
    with pytest.raises(LLMError, match=r"401.*invalid x-api-key"):
        client.complete(remote_cfg(), system="s", user="u")
    assert len(fake.requests) == 1


def test_http_error_with_plain_text_body(fake: FakeLLM) -> None:
    fake.queue((502, "Bad Gateway"))
    with pytest.raises(LLMError, match=r"502.*Bad Gateway"):
        client.complete(remote_cfg(), system="s", user="u")


def test_non_json_success_body_raises(fake: FakeLLM) -> None:
    fake.queue((200, "<html>oops</html>"))
    with pytest.raises(LLMError, match="non-JSON"):
        client.complete(remote_cfg(), system="s", user="u")


@pytest.mark.parametrize("status", [400, 404, 415, 422, 501])
def test_schema_rejection_retries_without_schema(fake: FakeLLM, status: int) -> None:
    fake.queue((status, {"error": "nope"}), "{}")
    resp = client.complete(remote_cfg(), system="s", user="u", json_schema=SCHEMA)
    assert len(fake.requests) == 2
    assert "response_format" in fake.requests[0]
    assert "response_format" not in fake.requests[1]
    assert resp.schema_mode == "text_fallback"


def test_no_retry_on_400_without_schema(fake: FakeLLM) -> None:
    fake.queue((400, {"error": {"message": "bad request"}}))
    with pytest.raises(LLMError, match="400"):
        client.complete(remote_cfg(), system="s", user="u")
    assert len(fake.requests) == 1


def test_still_failing_after_schema_retry_raises(fake: FakeLLM) -> None:
    fake.queue((400, {"error": "a"}), (400, {"error": {"message": "second"}}))
    with pytest.raises(LLMError, match="second"):
        client.complete(remote_cfg(), system="s", user="u", json_schema=SCHEMA)


def test_transport_error_becomes_llm_error(fake: FakeLLM) -> None:
    def boom(_: dict[str, Any]) -> Any:
        raise httpx.ConnectError("refused")

    fake.queue(boom)  # type: ignore[arg-type]
    with pytest.raises(LLMError, match="refused"):
        client.complete(remote_cfg(), system="s", user="u")


def test_disabled_config_raises(fake: FakeLLM) -> None:
    with pytest.raises(LLMError, match="no LLM configured"):
        client.complete(remote_cfg(enabled=False), system="s", user="u")
    with pytest.raises(LLMError, match="no LLM configured"):
        client.complete(LLMConfig(), system="s", user="u")
    assert fake.requests == []


# ---- extract_json -------------------------------------------------------------------------


def test_extract_json_plain() -> None:
    assert client.extract_json('{"a": 1}') == {"a": 1}
    assert client.extract_json("[1, 2]") == [1, 2]


def test_extract_json_fenced() -> None:
    assert client.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert client.extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_leading_and_trailing_prose() -> None:
    assert client.extract_json('Sure! Here you go:\n{"a": [1, 2]}\nHope that helps.') == {
        "a": [1, 2]
    }
    assert client.extract_json('Result: [{"a": 1}]') == [{"a": 1}]


def test_extract_json_garbage_raises() -> None:
    with pytest.raises(LLMError, match="not valid JSON"):
        client.extract_json("no json here")
    with pytest.raises(LLMError):
        client.extract_json("{broken")


# ---- the real _post over httpx.MockTransport ------------------------------------------------


class _MockClient(httpx.Client):
    """An `httpx.Client` that always uses a canned transport (no socket)."""

    handler: Any = None

    def __init__(self, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(type(self).handler)
        super().__init__(**kwargs)


def test_post_end_to_end_with_mock_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=wrap_text("openai_chat", '{"ok": true}'))

    _MockClient.handler = staticmethod(handler)
    monkeypatch.setattr(client.httpx, "Client", _MockClient)

    resp = client.complete(remote_cfg(), system="s", user="u")
    assert resp.text == '{"ok": true}'
    assert str(seen[0].url) == "https://api.example.com/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer sk-test-123"


def test_post_retries_once_on_503(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=wrap_text("openai_chat", "hi"))

    _MockClient.handler = staticmethod(handler)
    monkeypatch.setattr(client.httpx, "Client", _MockClient)
    monkeypatch.setattr(client.time, "sleep", lambda _: None)

    assert client.complete(remote_cfg(), system="s", user="u").text == "hi"
    assert len(calls) == 2


def test_post_non_json_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _MockClient.handler = staticmethod(lambda r: httpx.Response(401, text="Unauthorized"))
    monkeypatch.setattr(client.httpx, "Client", _MockClient)
    with pytest.raises(LLMError, match=r"401.*Unauthorized"):
        client.complete(remote_cfg(), system="s", user="u")
