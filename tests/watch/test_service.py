"""The background thread: off by default, scans on its schedule when switched on, "scan now"."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from personal_finance_analyzer.config import WatchConfig
from personal_finance_analyzer.db.session import make_session_factory
from personal_finance_analyzer.watch.scanner import FolderScanner
from personal_finance_analyzer.watch.service import WatchService
from tests.watch.support import add_bank, add_person, put_statement, records, statements

STATEMENT = "Alex/Statements/Fixture Bank/jan.pdf"


def _fast() -> WatchConfig:
    # Bypasses the 10 s floor so the test does not take minutes.
    return WatchConfig.model_construct(enabled=True, interval_seconds=0)


@contextmanager
def _service(
    engine: Engine, config: Callable[[], WatchConfig] | None = None
) -> Iterator[WatchService]:
    factory = make_session_factory(engine)
    service = WatchService(
        engine, factory, config=config, scanner=FolderScanner(factory, sleep=lambda _s: None)
    )
    service.start()
    try:
        yield service
    finally:
        service.stop()


def _until(check: Callable[[], bool], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def people(session: Session) -> None:
    add_person(session, "Alex", default=True)
    add_bank(session)


def test_watching_is_off_by_default() -> None:
    assert WatchConfig().enabled is False
    assert WatchConfig().interval_seconds == 60


def test_nothing_is_scanned_while_it_is_switched_off(
    engine: Engine, session: Session, people: None
) -> None:
    put_statement(STATEMENT)
    with _service(engine) as service:  # the real config: default, off
        time.sleep(0.5)
        assert service.status().enabled is False
        assert service.status().last_scan is None and service.status().next_scan_at is None
    assert statements(session) == [] and records(session) == {}


def test_when_switched_on_a_new_file_is_imported_within_two_scans(
    engine: Engine, session: Session, people: None
) -> None:
    put_statement(STATEMENT)
    with _service(engine, _fast) as service:
        assert _until(lambda: bool(statements(session)))
        assert statements(session)[0].stored_path == STATEMENT
        assert _until(lambda: service.status().last_scan is not None)
        status = service.status()
        assert status.enabled and status.problem is None


def test_a_file_dropped_while_running_is_picked_up(
    engine: Engine, session: Session, people: None
) -> None:
    with _service(engine, _fast) as service:
        assert _until(lambda: service.status().last_scan is not None)
        put_statement(STATEMENT)
        assert _until(lambda: bool(statements(session)))


def test_switching_on_at_runtime_takes_effect_without_a_restart(
    engine: Engine, session: Session, people: None
) -> None:
    state = {"on": False}
    put_statement(STATEMENT)

    def config() -> WatchConfig:
        return WatchConfig.model_construct(enabled=state["on"], interval_seconds=0)

    with _service(engine, config) as service:
        time.sleep(0.3)
        assert statements(session) == []
        state["on"] = True
        service.wake()
        assert _until(lambda: bool(statements(session)))


def test_scan_now_works_when_watching_is_off_and_reports_a_running_scan(
    engine: Engine, session: Session, people: None
) -> None:
    put_statement(STATEMENT)
    with _service(engine) as service:
        assert service.scan_now() is True
        assert service.wait_idle()
        status = service.status()
        assert status.last_scan is not None and status.last_scan.imported == 1
        assert statements(session)[0].stored_path == STATEMENT


def test_scan_now_does_not_start_a_second_scan(engine: Engine, people: None) -> None:
    with _service(engine) as service:
        with service._lock:  # pretend a scan is in progress
            service._running = True
        assert service.scan_now() is False
        assert service.status().running is True
        with service._lock:
            service._running = False


def test_nothing_runs_while_the_database_is_not_migrated() -> None:
    from sqlalchemy import create_engine

    from personal_finance_analyzer.db.session import make_session_factory as factory_for

    engine = create_engine("sqlite://")
    try:
        service = WatchService(engine, factory_for(engine), config=_fast)
        assert service.run_scan() is None
        problem = service.status().problem
        assert problem is not None and "migrate" in problem
    finally:
        engine.dispose()


def test_a_scan_does_not_run_while_paused(engine: Engine, people: None) -> None:
    factory = make_session_factory(engine)
    service = WatchService(engine, factory, config=_fast, paused=lambda: True)
    assert service.run_scan() is None
    assert service.status().last_scan is None


def test_stopping_the_service_ends_its_thread_promptly(engine: Engine) -> None:
    factory = make_session_factory(engine)
    service = WatchService(engine, factory)
    service.start()
    started = time.monotonic()
    service.stop()
    assert time.monotonic() - started < 5
