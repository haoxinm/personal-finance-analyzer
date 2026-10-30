"""The opt-in yearly tax-parameter check (`api/services/tax_params_update.py`). No real network:
every test injects a fake fetcher (and the suite's socket guard would fail any real request)."""

from __future__ import annotations

from datetime import date
from importlib import resources
from pathlib import Path

import pytest
import yaml

from spend_analyzer.api.services import tax_params_update as upd
from spend_analyzer.config import Settings, TaxConfig
from spend_analyzer.core import tax_params

ENABLED = Settings(tax=TaxConfig(auto_update_params=True))


def _bundled_text() -> str:
    return (
        resources.files("spend_analyzer.core")
        .joinpath("tax_data", "federal_params.yaml")
        .read_text(encoding="utf-8")
    )


def _remote(version: int, *, wage_base_2026: int = 190_000) -> str:
    raw = yaml.safe_load(_bundled_text())
    raw["params_version"] = version
    raw["years"][2026]["ss_wage_base"] = wage_base_2026
    return yaml.safe_dump(raw)


class FakeFetcher:
    def __init__(self, reply: str | upd.FetchError) -> None:
        self.reply = reply
        self.urls: list[str] = []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        if isinstance(self.reply, upd.FetchError):
            raise self.reply
        return self.reply


@pytest.fixture(autouse=True)
def _reset_flight() -> None:
    upd._in_flight = False


def _bundled_version() -> int:
    return tax_params.load_bundled().version


def test_request_is_the_fixed_plain_url() -> None:
    assert upd.PARAMS_URL.startswith("https://raw.githubusercontent.com/")
    assert "?" not in upd.PARAMS_URL and "#" not in upd.PARAMS_URL
    assert upd.PARAMS_URL.endswith("/src/spend_analyzer/core/tax_data/federal_params.yaml")
    assert (Path(__file__).resolve().parents[2] / "src/spend_analyzer/core/tax_data").is_dir()


def test_newer_valid_file_is_cached_and_used(home: Path) -> None:
    fetch = FakeFetcher(_remote(_bundled_version() + 1))
    result = upd.check_for_update(fetch=fetch, today=date(2027, 2, 1))
    assert result.outcome == "updated"
    assert fetch.urls == [upd.PARAMS_URL]
    assert tax_params.resolve().source == "cached"
    state = tax_params.read_check_state()
    assert (state.last_checked, state.outcome) == (date(2027, 2, 1), "updated")


def test_same_or_older_version_is_not_installed(home: Path) -> None:
    result = upd.check_for_update(fetch=FakeFetcher(_remote(_bundled_version())))
    assert result.outcome == "current"
    older = upd.check_for_update(fetch=FakeFetcher(_remote(max(_bundled_version() - 1, 1))))
    assert older.outcome == "current"
    assert not tax_params.cached_path().exists()


def test_must_beat_the_cached_copy_too(home: Path) -> None:
    tax_params.install_cached(_remote(_bundled_version() + 3))
    assert upd.check_for_update(fetch=FakeFetcher(_remote(_bundled_version() + 2))).outcome == (
        "current"
    )
    assert upd.check_for_update(fetch=FakeFetcher(_remote(_bundled_version() + 4))).outcome == (
        "updated"
    )


@pytest.mark.parametrize(
    "body", ["", "<html>not yaml</html>", "params_version: 99\nyears: {}\n", "a: [broken"]
)
def test_invalid_download_is_rejected_and_nothing_is_written(home: Path, body: str) -> None:
    result = upd.check_for_update(fetch=FakeFetcher(body))
    assert result.outcome == "rejected"
    assert not tax_params.cached_path().exists()
    assert tax_params.resolve().source == "bundled"


def test_network_error_falls_back_silently_but_still_records_the_check(home: Path) -> None:
    result = upd.check_for_update(
        fetch=FakeFetcher(upd.FetchError("ConnectError")), today=date(2027, 5, 5)
    )
    assert result.outcome == "unreachable"
    assert tax_params.resolve().source == "bundled"
    assert tax_params.read_check_state().last_checked == date(2027, 5, 5)


def test_disabled_by_default_never_fetches(home: Path) -> None:
    fetch = FakeFetcher(_remote(_bundled_version() + 1))
    assert upd.maybe_check_in_background(Settings(), fetch=fetch, wait=True) is False
    assert fetch.urls == []
    assert not tax_params.tax_params_dir().exists()


def test_checks_at_most_once_per_calendar_year(home: Path) -> None:
    fetch = FakeFetcher(_remote(_bundled_version() + 1))
    assert upd.maybe_check_in_background(ENABLED, fetch=fetch, today=date(2027, 1, 10), wait=True)
    assert len(fetch.urls) == 1
    # later the same year (any number of times): no request
    for day in (date(2027, 1, 10), date(2027, 6, 1), date(2027, 12, 31)):
        assert not upd.maybe_check_in_background(ENABLED, fetch=fetch, today=day, wait=True)
    assert len(fetch.urls) == 1
    # a new calendar year checks again
    assert upd.maybe_check_in_background(ENABLED, fetch=fetch, today=date(2028, 1, 1), wait=True)
    assert len(fetch.urls) == 2


def test_failed_attempt_is_not_retried_in_the_same_year(home: Path) -> None:
    fetch = FakeFetcher(upd.FetchError("ConnectError"))
    assert upd.maybe_check_in_background(ENABLED, fetch=fetch, today=date(2027, 3, 1), wait=True)
    assert not upd.maybe_check_in_background(
        ENABLED, fetch=fetch, today=date(2027, 3, 2), wait=True
    )
    assert len(fetch.urls) == 1


def test_manual_check_runs_even_if_checked_this_year(home: Path) -> None:
    fetch = FakeFetcher(_remote(_bundled_version()))
    upd.check_for_update(fetch=fetch, today=date(2027, 1, 1))
    upd.check_for_update(fetch=fetch, today=date(2027, 1, 2))
    assert len(fetch.urls) == 2
    assert tax_params.read_check_state().last_checked == date(2027, 1, 2)


def test_only_one_background_check_in_flight(home: Path) -> None:
    fetch = FakeFetcher(_remote(_bundled_version()))
    upd._in_flight = True
    assert not upd.maybe_check_in_background(
        ENABLED, fetch=fetch, today=date(2027, 1, 1), wait=True
    )
    assert fetch.urls == []


def test_cache_is_used_on_later_loads_without_fetching(home: Path) -> None:
    upd.check_for_update(fetch=FakeFetcher(_remote(_bundled_version() + 1, wage_base_2026=191_000)))
    from spend_analyzer.core import tax_calc

    params = tax_calc.params_for(2026)
    assert params is not None and params.ss_wage_base_minor == 191_000 * 100
    # a fresh resolve (as after a restart) reads the stored copy and makes no request
    tax_params._resolved = None
    again = tax_calc.params_for(2026)
    assert again is not None and again.ss_wage_base_minor == 191_000 * 100


def test_fetch_text_refuses_oversized_and_wraps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(upd.PARAMS_URL)
        assert not request.url.query
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(200, content=b"x" * (tax_params.MAX_PARAMS_BYTES + 10))

    real_client = httpx.Client

    def client_with_transport(**kwargs: object) -> httpx.Client:
        assert kwargs.get("trust_env") is False and kwargs.get("follow_redirects") is False
        return real_client(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(upd.httpx, "Client", client_with_transport)
    with pytest.raises(upd.FetchError, match="too large"):
        upd.fetch_text(upd.PARAMS_URL)

    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    monkeypatch.setattr(
        upd.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(not_found), **kw),
    )
    with pytest.raises(upd.FetchError, match="HTTPStatusError"):
        upd.fetch_text(upd.PARAMS_URL)
