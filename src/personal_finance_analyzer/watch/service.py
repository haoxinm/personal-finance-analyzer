"""The background half of folder imports: a thread inside `pfa serve` that runs a scan every
``[watch] interval_seconds`` while ``[watch] enabled`` is on, plus "scan now" and the status the
Import page shows.

The thread always runs (it is idle while watching is off) and re-reads the settings on every
cycle, so switching it on or changing the interval in Settings takes effect without a restart.
Scans run on this thread (or on a short-lived one for "scan now"), never on the event loop, and
use their own database sessions. Nothing here starts before the database is migrated: until the
schema is current a scan is skipped and the status says so.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from personal_finance_analyzer.config import WatchConfig, load_settings
from personal_finance_analyzer.core.paths import documents_dir
from personal_finance_analyzer.db.migrate import is_at_head
from personal_finance_analyzer.watch.scanner import MANUAL_SETTLE_S, FolderScanner, ScanReport

logger = logging.getLogger(__name__)

#: How often an idle (or waiting) thread re-reads the settings, seconds.
_POLL_S = 3.0


@dataclass(frozen=True)
class ScanSummary:
    started_at: str
    finished_at: str | None
    scanned: int
    imported: int
    known: int
    failed: int
    ignored: int
    waiting: int
    busy: bool
    error: str | None

    @classmethod
    def of(cls, report: ScanReport) -> ScanSummary:
        return cls(
            started_at=report.started_at,
            finished_at=report.finished_at,
            scanned=report.scanned,
            imported=report.imported,
            known=report.known,
            failed=report.failed,
            ignored=report.ignored,
            waiting=report.waiting,
            busy=report.busy,
            error=report.error,
        )


@dataclass(frozen=True)
class WatchStatus:
    enabled: bool
    interval_seconds: int
    documents_dir: str
    running: bool
    next_scan_at: str | None
    last_scan: ScanSummary | None
    #: Set when scans cannot run at all (database not migrated, folder missing...).
    problem: str | None


class WatchService:
    """Owns one `FolderScanner` and the thread that drives it."""

    def __init__(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        *,
        config: Callable[[], WatchConfig] | None = None,
        scanner: FolderScanner | None = None,
        paused: Callable[[], bool] | None = None,
    ) -> None:
        self._engine = engine
        #: True while something (a backup restore) needs the data directory to itself.
        self._paused = paused if paused is not None else (lambda: False)
        self._scanner = scanner if scanner is not None else FolderScanner(session_factory)
        self._config = config if config is not None else (lambda: load_settings().watch)
        self._lock = threading.Lock()  # guards the fields below
        self._scan_mutex = threading.Lock()  # one scan at a time in this process
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._manual: threading.Thread | None = None
        self._last: ScanSummary | None = None
        self._next_at: float | None = None  # time.monotonic() of the next automatic scan
        self._next_wall: datetime | None = None
        self._running = False
        self._problem: str | None = None
        self._migrated = False

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pfa-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=10)
        manual = self._manual
        if manual is not None:
            manual.join(timeout=10)

    def wake(self) -> None:
        """Re-read the settings now (they were just saved)."""
        self._wake.set()

    # ------------------------------------------------------------------ status

    def status(self) -> WatchStatus:
        cfg = self._config()
        with self._lock:
            next_at = self._next_wall.isoformat() if cfg.enabled and self._next_wall else None
            return WatchStatus(
                enabled=cfg.enabled,
                interval_seconds=cfg.interval_seconds,
                documents_dir=str(documents_dir()),
                running=self._running,
                next_scan_at=next_at,
                last_scan=self._last,
                problem=self._problem,
            )

    @property
    def scanner(self) -> FolderScanner:
        return self._scanner

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until no scan is running (tests, and a caller that wants the result)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            manual = self._manual
            with self._lock:
                running = self._running
            if not running and (manual is None or not manual.is_alive()):
                return True
            time.sleep(0.02)
        return False

    # ------------------------------------------------------------------ scanning

    def scan_now(self) -> bool:
        """Start a scan in the background right now, whether or not watching is switched on (it
        is the user's explicit request). Returns False when one is already running."""
        with self._lock:
            if self._running:
                return False
            self._running = True  # claimed before the thread starts: no second click slips in
        thread = threading.Thread(
            target=self._run_claimed, kwargs={"manual": True}, name="pfa-watch-now", daemon=True
        )
        self._manual = thread
        thread.start()
        return True

    def run_scan(self, *, manual: bool = False) -> ScanSummary | None:
        """Run one scan on the calling thread (None when one is already running here)."""
        with self._lock:
            if self._running:
                return None
            self._running = True
        return self._run_claimed(manual=manual)

    def _run_claimed(self, *, manual: bool) -> ScanSummary | None:
        try:
            with self._scan_mutex:
                if self._paused() or not self._ready():
                    return None
                report = self._scanner.scan(
                    settle_seconds=MANUAL_SETTLE_S if manual else 0.0, should_stop=self._paused
                )
                summary = ScanSummary.of(report)
                # The result is published before `running` clears, so whoever sees the scan
                # finished also sees what it did.
                with self._lock:
                    self._last = summary
                    if report.error:
                        self._problem = report.error
                    elif not report.busy:
                        self._problem = None
        finally:
            with self._lock:
                self._running = False
        if report.imported or report.failed:
            logger.info(
                "folder scan: %d imported, %d failed, %d waiting",
                report.imported,
                report.failed,
                report.waiting,
            )
        return summary

    def _ready(self) -> bool:
        """The database must be at the current schema before anything is imported."""
        if self._migrated:
            return True
        try:
            self._migrated = is_at_head(self._engine)
        except Exception as exc:
            self._set_problem(f"The database cannot be read yet ({type(exc).__name__}).")
            return False
        if not self._migrated:
            self._set_problem("The database is not up to date: run `pfa migrate`, then restart.")
        return self._migrated

    def _set_problem(self, text: str) -> None:
        with self._lock:
            self._problem = text

    # ------------------------------------------------------------------ the thread

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # the thread must survive anything
                logger.warning("folder watch cycle failed", exc_info=True)
                self._stop.wait(_POLL_S)

    def _tick(self) -> None:
        cfg = self._config()
        now = time.monotonic()
        if not cfg.enabled:
            with self._lock:
                self._next_at = None
                self._next_wall = None
            self._sleep(_POLL_S)
            return
        with self._lock:
            if self._next_at is None:  # just switched on (or the server just started): scan now
                self._next_at = now
                self._next_wall = datetime.now(UTC)
            elif self._next_at > now + cfg.interval_seconds:  # the interval was shortened
                self._next_at = now + cfg.interval_seconds
                self._next_wall = datetime.now(UTC) + timedelta(seconds=cfg.interval_seconds)
            due = self._next_at
        if now < due:
            self._sleep(min(due - now, _POLL_S))
            return
        self.run_scan()
        interval = float(self._config().interval_seconds)
        with self._lock:
            self._next_at = time.monotonic() + interval
            self._next_wall = datetime.now(UTC) + timedelta(seconds=interval)

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()
