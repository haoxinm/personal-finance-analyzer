"""`[watch]` settings: defaults, the file, the environment shortcuts, clamping."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_finance_analyzer.config import (
    WATCH_MAX_INTERVAL_S,
    WATCH_MIN_INTERVAL_S,
    Settings,
    WatchConfig,
    env_overridden_fields,
    env_override_var,
    load_file_settings,
    load_settings,
    save_settings,
)
from personal_finance_analyzer.core.paths import config_path


def test_off_by_default_with_a_sixty_second_interval() -> None:
    watch = load_settings().watch
    assert watch.enabled is False and watch.interval_seconds == 60


def test_the_settings_round_trip_through_config_toml() -> None:
    save_settings(Settings(watch=WatchConfig(enabled=True, interval_seconds=120)))
    assert "[watch]" in config_path().read_text()
    loaded = load_settings().watch
    assert (loaded.enabled, loaded.interval_seconds) == (True, 120)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1, WATCH_MIN_INTERVAL_S), (10, 10), (45, 45), (10**9, WATCH_MAX_INTERVAL_S), (-5, 10)],
)
def test_the_interval_is_clamped_not_rejected(raw: int, expected: int) -> None:
    assert WatchConfig(interval_seconds=raw).interval_seconds == expected


def test_a_hand_edited_out_of_range_interval_still_loads(home: Path) -> None:
    config_path().write_text("[watch]\nenabled = true\ninterval_seconds = 2\n")
    watch = load_settings().watch
    assert watch.enabled is True and watch.interval_seconds == WATCH_MIN_INTERVAL_S


def test_environment_variables_override_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    save_settings(Settings(watch=WatchConfig(enabled=False, interval_seconds=90)))
    monkeypatch.setenv("PFA_WATCH_ENABLED", "true")
    monkeypatch.setenv("PFA_WATCH_INTERVAL", "30")
    watch = load_settings().watch
    assert (watch.enabled, watch.interval_seconds) == (True, 30)
    # The file itself is untouched, and the overrides are reported as such.
    assert load_file_settings().watch.enabled is False
    assert ("watch", "enabled") in env_overridden_fields()
    assert env_override_var("watch", "interval_seconds") == "PFA_WATCH_INTERVAL"


def test_the_generic_double_underscore_form_works_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_WATCH__ENABLED", "1")
    assert load_settings().watch.enabled is True


def test_a_bad_interval_in_the_environment_does_not_break_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PFA_WATCH_INTERVAL", "soon")
    assert load_settings().watch.interval_seconds == 60


def test_the_environment_wins_even_to_switch_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    save_settings(Settings(watch=WatchConfig(enabled=True)))
    monkeypatch.setenv("PFA_WATCH_ENABLED", "false")
    assert load_settings().watch.enabled is False
