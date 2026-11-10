"""Shared "is the LLM reachable?" check, used by `POST /api/settings/test-llm`,
`pfa llm test`, and `pfa doctor`.

Goes through `classify_batch` (the egress boundary) — never the transport directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from personal_finance_analyzer.config import LLMConfig


@dataclass(frozen=True, slots=True)
class ConnectionResult:
    ok: bool
    detail: str
    latency_ms: int | None = None


def explain_not_configured(cfg: LLMConfig) -> str:
    """Why `cfg.mode == "none"`, and how to fix it."""
    problems: list[str] = []
    if not cfg.enabled:
        problems.append("the LLM is turned off")
    if not cfg.base_url.strip():
        problems.append("no address (base URL) is set")
    why = "; ".join(problems) or "the LLM is not configured"
    return (
        f"LLM mode is 'none': {why}. Turn on 'Use an LLM' and fill in the address "
        "(the model is optional for single-model local servers), then Test "
        "(CLI: `pfa llm set --base-url ... --enable`)."
    )


def check_connection(cfg: LLMConfig) -> ConnectionResult:
    """One real round trip with a harmless one-row batch. Never raises."""
    if cfg.mode == "none":
        return ConnectionResult(False, explain_not_configured(cfg))
    try:
        from personal_finance_analyzer.classify.llm.egress import classify_batch

        result = classify_batch(["Test Merchant"], cfg)
    except Exception as exc:
        return ConnectionResult(False, str(exc))
    return ConnectionResult(
        True,
        f"Connected to {cfg.resolved_url} ({cfg.mode}) — model replied in {result.latency_ms} ms",
        result.latency_ms,
    )
