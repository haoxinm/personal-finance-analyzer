"""A fake LLM endpoint for tests: replaces `personal_finance_analyzer.llm.client._post`, records every
request body it receives, and answers in whichever wire format the config asks for. Never
opens a socket (§6.5)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from personal_finance_analyzer.config import LLMConfig
from personal_finance_analyzer.llm import client

Reply = str | tuple[int, Any] | Callable[[dict[str, Any]], "str | tuple[int, Any]"]


def wrap_text(api_format: str, text: str, *, tokens_in: int = 10, tokens_out: int = 20) -> Any:
    if api_format == "anthropic_messages":
        return {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        }
    if api_format == "openai_responses":
        return {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": text}]},
            ],
            "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
        }
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }


@dataclass
class FakeLLM:
    replies: list[Reply] = field(default_factory=list)
    default: Reply | None = None
    requests: list[dict[str, Any]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)

    def queue(self, *replies: Reply) -> FakeLLM:
        self.replies.extend(replies)
        return self

    def _post(self, cfg: LLMConfig, body: dict[str, Any]) -> tuple[int, Any]:
        self.requests.append(body)
        self.urls.append(cfg.resolved_url)
        self.headers.append(client._headers(cfg))
        if self.replies:
            reply = self.replies.pop(0)
        elif self.default is not None:
            reply = self.default
        else:
            raise AssertionError("FakeLLM: no reply queued")
        if callable(reply):
            reply = reply(body)
        if isinstance(reply, tuple):
            return reply
        return 200, wrap_text(cfg.api_format, reply)

    @property
    def last_user_text(self) -> str:
        body = self.requests[-1]
        if "input" in body:
            return str(body["input"][0]["content"])
        messages = body["messages"]
        return str(messages[-1]["content"])


def install(monkeypatch: pytest.MonkeyPatch, fake: FakeLLM | None = None) -> FakeLLM:
    fake = fake or FakeLLM()
    monkeypatch.setattr(client, "_post", fake._post)
    return fake


def remote_cfg(**overrides: Any) -> LLMConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "base_url": "https://api.example.com",
        "api_format": "openai_chat",
        "model": "test-model",
    }
    base.update(overrides)
    return LLMConfig(**base)
