"""Opt-in, once-a-year refresh of the federal tax parameter file (`core/tax_params.py`).

**This is the only module that may make this request** (`tests/test_architecture_guards.py`). It is
off unless the user sets ``[tax] auto_update_params = true`` or runs ``pfa tax params
update`` / presses *Check for updates* (an explicit action). What is sent is a plain HTTPS ``GET`` of
one fixed URL, `PARAMS_URL`: no query string, cookies, credentials, proxy or netrc lookup, and no
user data of any kind (not the user's tax data, not the installed version). GitHub sees an IP address
and the library's default ``User-Agent``, like any download.

**Gating.** At most one automatic check per calendar year: the date of the last attempt is stored in
``<data dir>/tax_params/state.json`` and recorded *before* the request, whatever its outcome, so an
offline machine does not retry on every page view (a manual check always runs and also records the
date). The automatic check runs in a daemon thread, never blocks a request, and fails silently with
an info log.

**Acceptance.** The download is never trusted wholesale and never replaces the app's own figures.
The text must pass the same schema loader as the bundled file
(`core/tax_params.parse_params_text`), and it may only ADD tax years that the bundled file lacks
(a bundled year always comes from the app; corrections ship with an app update). Every added year
must follow a known year and pass the range, structure and year-over-year checks of
`core/tax_params.added_years`; one violation rejects the whole download. Only then is the file
stored (mode 0600) as the cached copy; anything else leaves the data directory untouched. See
`core/tax_params.resolve` for how the years are merged.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

import httpx

from personal_finance_analyzer.config import Settings
from personal_finance_analyzer.core import tax_params

_log = logging.getLogger(__name__)

#: The one URL ever requested: the same file the package bundles, on the project's main branch.
PARAMS_URL = (
    "https://raw.githubusercontent.com/haoxinm/personal-finance-analyzer/main/"
    "src/personal_finance_analyzer/core/tax_data/federal_params.yaml"
)
TIMEOUT_S = 10.0

UpdateOutcome = Literal["updated", "current", "rejected", "unreachable"]

#: Returns the text at a URL; raises `FetchError`. Injected in tests: no test touches the network.
Fetcher = Callable[[str], str]


class FetchError(Exception):
    """The file could not be downloaded (network error, non-200, too large, not UTF-8 text)."""


def fetch_text(url: str) -> str:
    """GET `url` and return the body as text.

    No cookies, auth, proxy or netrc (``trust_env=False``), no redirects, a short timeout and a size
    cap (`tax_params.MAX_PARAMS_BYTES`).

    Raises:
        FetchError: any failure.
    """
    try:
        with (
            httpx.Client(timeout=TIMEOUT_S, trust_env=False, follow_redirects=False) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > tax_params.MAX_PARAMS_BYTES:
                    raise FetchError("response too large")
    except httpx.HTTPError as exc:
        raise FetchError(type(exc).__name__) from None
    try:
        return bytes(body).decode("utf-8")
    except UnicodeDecodeError:
        raise FetchError("response is not UTF-8 text") from None


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """One check: ``updated`` (new tax years are now cached), ``current`` (nothing to add),
    ``rejected`` (downloaded but invalid or implausible) or ``unreachable``. `detail` is fixed text, never the
    response body."""

    outcome: UpdateOutcome
    detail: str
    version: int | None = None


def check_for_update(*, fetch: Fetcher | None = None, today: date | None = None) -> UpdateResult:
    """Run one check now and record `today` (default: the local date) as the last check.

    Never raises for a network or content problem (see `UpdateResult`).
    """
    day = today if today is not None else date.today()
    fetcher = fetch if fetch is not None else fetch_text
    result = _check(fetcher, day)
    tax_params.record_check(day, result.outcome)
    _log.info("tax parameter update check: %s", result.outcome)
    return result


def _check(fetch: Fetcher, today: date) -> UpdateResult:
    try:
        text = fetch(PARAMS_URL)
    except FetchError as exc:
        return UpdateResult("unreachable", f"Could not download the file ({exc}).")
    try:
        candidate = tax_params.parse_params_text(text, source="cached")
        bundled_years = tax_params.load_bundled().years
        added = tax_params.added_years(bundled_years, candidate, latest_year=today.year + 1)
    except ValueError as exc:
        return UpdateResult("rejected", f"The downloaded file was rejected: {exc}")
    have = _cached_additions()
    if not added or all(have.get(year) == params for year, params in added.items()):
        return UpdateResult(
            "current",
            "No new tax years are available (the app's own figures always take precedence).",
            version=tax_params.load_bundled().version,
        )
    try:
        tax_params.install_cached(text)
    except OSError:
        return UpdateResult("rejected", "The downloaded file could not be saved.")
    return UpdateResult(
        "updated",
        f"Added tax year(s) {', '.join(str(y) for y in sorted(added))}.",
        version=candidate.version,
    )


def _cached_additions() -> dict[int, tax_params.FederalParams]:
    """The years the already-cached download currently contributes (empty when none or invalid)."""
    cached = tax_params.read_cached()
    if cached is None:
        return {}
    try:
        return tax_params.added_years(tax_params.load_bundled().years, cached)
    except ValueError:
        return {}


# --- automatic, once a year ------------------------------------------------------------------------

_lock = threading.Lock()
_in_flight = False


def is_due(today: date | None = None) -> bool:
    """True when no check has been recorded in `today`'s calendar year."""
    day = today if today is not None else date.today()
    last = tax_params.read_check_state().last_checked
    return last is None or last.year != day.year


def maybe_check_in_background(
    settings: Settings,
    *,
    fetch: Fetcher | None = None,
    today: date | None = None,
    wait: bool = False,
) -> bool:
    """Start the yearly check when it is enabled and due; returns whether one was started.

    A no-op unless ``settings.tax.auto_update_params``. The attempt is recorded before the request
    (see the module docstring), so concurrent calls start at most one. `wait` joins the worker
    (tests); otherwise the check runs in a daemon thread and the caller never waits.
    """
    global _in_flight
    if not settings.tax.auto_update_params:
        return False
    day = today if today is not None else date.today()
    with _lock:
        if _in_flight or not is_due(day):
            return False
        _in_flight = True
        try:
            tax_params.record_check(day, "pending")
        except OSError:
            _in_flight = False  # cannot persist the gate: do not risk a request per page view
            return False

    def run() -> None:
        global _in_flight
        try:
            check_for_update(fetch=fetch, today=day)
        except OSError:
            _log.info("tax parameter update check could not be saved")
        finally:
            with _lock:
                _in_flight = False

    worker = threading.Thread(target=run, name="tax-params-update", daemon=True)
    worker.start()
    if wait:
        worker.join()
    return True
