"""Tests for `classify/llm/egress.py` (I1, I2, I10; §3.7)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from personal_finance_analyzer.classify.llm import egress
from personal_finance_analyzer.config import LLMConfig
from personal_finance_analyzer.core.errors import EgressViolation, LLMError
from tests.llm_fakes import FakeLLM, install, remote_cfg

_LLM_ROOT = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "personal_finance_analyzer"
    / "classify"
    / "llm"
)

#: I1b: these column names must never appear in any module under classify/llm/, because they
#: name the local-only fields that must never be egressed.
_LOCAL_ONLY_TERMS = (
    "accounts.mask",
    "accounts.issuer_id",
    "issuers.name",
    "issuers.slug",
    "issuers.match_terms",
    "description_raw",
    "transactions.issuer_category",
    "users.name",
    "pii_aliases",
    "tax_documents",
    "tax_forms.payer_name",
    "payer_tin_last4",
    "account_last4",
    "income_items",
    "tax_items",
    "ParsedTaxForm",
    "balance_entries",
    "balance_minor",
    "BalanceEntry",
    "net_worth",
)


def _items_payload(*items: dict[str, Any]) -> str:
    return json.dumps({"items": list(items)})


def _item(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 1,
        "category": "grocery",
        "subcategory": "grocery_stores",
        "merchant_canonical": "Trader Joes",
        "confidence": 0.95,
        "is_online_store": False,
    }
    base.update(overrides)
    return base


def _cfg(**overrides: Any) -> LLMConfig:
    return remote_cfg(**overrides)


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_llm_api_key` checks the env var first, so setting it avoids ever touching the real
    (and here, backend-less) OS keyring."""
    monkeypatch.setenv("PFA_LLM_API_KEY", "sk-test-key-not-real")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    return install(monkeypatch)


def test_build_csv_header_and_rows() -> None:
    payload = egress.build_csv([(1, "TRADER JOES #123 SEATTLE WA"), (2, "DELTA AIR LINES")])
    assert payload == "id,description\n1,TRADER JOES #123 SEATTLE WA\n2,DELTA AIR LINES\n"


@pytest.mark.parametrize(
    "bad_description",
    [
        "CARD 4111111111111111",  # long digit run
        "contact us at billing@example.com",  # email
        "call 555-123-4567 now",  # phone
        "charged $12.34 today",  # currency amount
        "posted 2024-01-15",  # ISO date
    ],
)
def test_assert_safe_raises_on_forbidden_patterns(bad_description: str) -> None:
    payload = egress.build_csv([(1, bad_description)])
    with pytest.raises(EgressViolation):
        egress.assert_safe(payload)


def test_assert_safe_allows_store_numbers() -> None:
    # Short store numbers (#1234) survive normalization and are intentionally safe (§3.7).
    payload = egress.build_csv([(1, "TRADER JOES #1234 SEATTLE WA")])
    egress.assert_safe(payload)  # must not raise


def test_preview_payload_makes_no_network_call(fake: FakeLLM) -> None:
    payload = egress.preview_payload(["TRADER JOES #123", "DELTA AIR LINES"])
    assert payload == egress.build_csv([(1, "TRADER JOES #123"), (2, "DELTA AIR LINES")])
    assert fake.requests == []


def test_preview_payload_raises_on_violation() -> None:
    with pytest.raises(EgressViolation):
        egress.preview_payload(["email me at a@b.com"])


def test_classify_batch_raises_when_no_provider_configured(fake: FakeLLM) -> None:
    with pytest.raises(LLMError, match="no provider configured"):
        egress.classify_batch(["TRADER JOES"], LLMConfig())
    assert fake.requests == []


def test_classify_batch_raises_when_disabled_or_blank(fake: FakeLLM) -> None:
    with pytest.raises(LLMError, match="no provider configured"):
        egress.classify_batch(["TRADER JOES"], _cfg(enabled=False))
    with pytest.raises(LLMError, match="no provider configured"):
        egress.classify_batch(["TRADER JOES"], _cfg(base_url=""))
    assert fake.requests == []


def test_classify_batch_sends_response_format_json_schema(fake: FakeLLM) -> None:
    fake.queue(_items_payload(_item()))

    result = egress.classify_batch(["TRADER JOES #123"], _cfg())

    assert result.schema_mode == "json_schema"
    assert fake.requests[0]["response_format"]["type"] == "json_schema"
    assert result.status == "ok"
    assert result.missing_ids == ()
    assert len(result.items) == 1
    assert result.items[0].merchant_canonical == "Trader Joes"
    assert result.cost_usd == 0.0
    assert result.tokens_in == 10
    assert result.tokens_out == 20
    assert result.provider == "openai_chat"
    assert result.model == "test-model"
    assert result.row_count == 1
    # Only the CSV payload leaves the machine.
    assert fake.last_user_text == egress.build_csv([(1, "TRADER JOES #123")])


def test_classify_batch_schema_rejected_retries_without_schema(fake: FakeLLM) -> None:
    fake.queue((400, {"error": {"message": "response_format unsupported"}}))
    fake.queue(_items_payload(_item()))

    result = egress.classify_batch(["TRADER JOES"], _cfg())

    assert len(fake.requests) == 2
    assert "response_format" in fake.requests[0]
    assert "response_format" not in fake.requests[1]
    assert result.schema_mode == "text_fallback"
    assert result.status == "ok"


def test_classify_batch_structured_output_off_sends_no_response_format(fake: FakeLLM) -> None:
    fake.queue(_items_payload())

    result = egress.classify_batch([], _cfg(structured_output=False))

    assert len(fake.requests) == 1
    assert "response_format" not in fake.requests[0]
    assert result.schema_mode == "text_fallback"
    assert result.row_count == 0


@pytest.mark.parametrize("api_format", ["openai_chat", "openai_responses", "anthropic_messages"])
def test_classify_batch_works_for_every_api_format(fake: FakeLLM, api_format: str) -> None:
    fake.queue(_items_payload(_item()))
    result = egress.classify_batch(["TRADER JOES"], _cfg(api_format=api_format))
    assert result.status == "ok"
    assert result.provider == api_format


def test_classify_batch_rejects_out_of_taxonomy_category(fake: FakeLLM) -> None:
    fake.queue(_items_payload(_item(category="not_a_real_category", subcategory="whatever")))

    result = egress.classify_batch(["MYSTERY MERCHANT"], _cfg())

    assert result.items == ()
    assert result.missing_ids == (1,)
    assert result.status == "partial"


def test_classify_batch_rejects_subcategory_not_in_category(fake: FakeLLM) -> None:
    # "flights" is a valid subcategory, but of a different category.
    fake.queue(_items_payload(_item(subcategory="flights")))

    result = egress.classify_batch(["MYSTERY MERCHANT"], _cfg())
    assert result.missing_ids == (1,)


def test_classify_batch_accepts_dynamic_online_shopping_slug(fake: FakeLLM) -> None:
    fake.queue(
        _items_payload(
            _item(
                category="online_shopping",
                subcategory="yami",
                merchant_canonical="Yamibuy",
                is_online_store=True,
            )
        )
    )

    result = egress.classify_batch(["YAMIBUY"], _cfg())
    assert result.status == "ok"
    assert result.items[0].subcategory == "yami"


def test_classify_batch_duplicate_id_treated_as_missing(fake: FakeLLM) -> None:
    fake.queue(_items_payload(_item(), _item(category="fuel", subcategory="gasoline")))

    result = egress.classify_batch(["SOMETHING"], _cfg())
    assert result.items == ()
    assert result.missing_ids == (1,)


def test_classify_batch_request_failure_raises_llm_error(
    monkeypatch: pytest.MonkeyPatch, fake: FakeLLM
) -> None:
    def _raise(_: dict[str, Any]) -> Any:
        raise httpx.ConnectError("connection refused")

    fake.queue(_raise)  # type: ignore[arg-type]

    with pytest.raises(LLMError):
        egress.classify_batch(["X"], _cfg())


def test_classify_batch_http_error_raises_llm_error(fake: FakeLLM) -> None:
    fake.queue((401, {"error": {"message": "bad key"}}))
    with pytest.raises(LLMError, match="401"):
        egress.classify_batch(["X"], _cfg())


def test_classify_batch_malformed_json_raises_llm_error(fake: FakeLLM) -> None:
    fake.queue("not json at all")
    with pytest.raises(LLMError):
        egress.classify_batch(["X"], _cfg())


def test_classify_batch_strips_markdown_code_fence(fake: FakeLLM) -> None:
    fake.queue("```json\n" + _items_payload(_item()) + "\n```")

    result = egress.classify_batch(["X"], _cfg())
    assert result.status == "ok"
    assert result.items[0].id == 1


def test_request_sha256_is_deterministic_for_same_descriptions(fake: FakeLLM) -> None:
    fake.default = _items_payload()

    r1 = egress.classify_batch(["A", "B"], _cfg())
    r2 = egress.classify_batch(["A", "B"], _cfg())
    assert r1.request_sha256 == r2.request_sha256

    payload = egress.build_csv([(1, "A"), (2, "B")])
    assert r1.request_sha256 == hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_egress_violation_blocks_before_any_request(fake: FakeLLM) -> None:
    with pytest.raises(EgressViolation):
        egress.classify_batch(["CARD 4111111111111111"], _cfg())
    assert fake.requests == []


def test_llm_package_never_mentions_local_only_columns() -> None:
    """I1b: a CI guard specific to classify/llm/ — none of the local-only field names may
    appear anywhere in this module tree, not even in a comment or docstring."""
    for path in sorted(_LLM_ROOT.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for term in _LOCAL_ONLY_TERMS:
            assert term not in text, f"{path} mentions local-only field {term!r} (I1b)"
