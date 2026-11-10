"""Quiesces the running server for `POST /api/backup/restore`.

A restore swaps the data directory under the server, so while it happens no other `/api` request
may be touching the database. `RestoreGate` counts in-flight requests; `begin_exclusive` turns new
ones away with 503 (`RestoreGateMiddleware`) and waits for the running ones to finish, and
`end_exclusive` lets traffic through again. The job-progress stream (`.../events`) is exempt: it is
long-lived, only polls, and ends by itself once the restored database has no such job.
"""

from __future__ import annotations

import threading
import time

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from personal_finance_analyzer.backup.errors import BackupBusyError


class RestoreGate:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._in_flight = 0
        self._exclusive = False

    def enter(self) -> bool:
        """Count one request in; False (not counted) while a restore is under way."""
        with self._cond:
            if self._exclusive:
                return False
            self._in_flight += 1
            return True

    def leave(self) -> None:
        with self._cond:
            self._in_flight -= 1
            self._cond.notify_all()

    def begin_exclusive(self, *, timeout: float = 15.0) -> None:
        """Stop new requests and wait until only the calling request (itself counted in) is left.

        Raises:
            BackupBusyError: another restore is running, or other requests did not finish in time.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            if self._exclusive:
                raise BackupBusyError("another restore is already in progress")
            self._exclusive = True
            while self._in_flight > 1:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._exclusive = False
                    raise BackupBusyError("the app is busy with other requests; try again")
                self._cond.wait(remaining)

    @property
    def exclusive(self) -> bool:
        """Whether a restore is under way (background work must not start meanwhile)."""
        with self._cond:
            return self._exclusive

    def end_exclusive(self) -> None:
        with self._cond:
            self._exclusive = False


class RestoreGateMiddleware:
    """Pure ASGI (not `BaseHTTPMiddleware`) so a request counts as in flight until its response
    body has been fully sent, not merely until its headers were."""

    def __init__(self, app: ASGIApp, *, gate: RestoreGate) -> None:
        self.app = app
        self._gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api") or path.endswith("/events"):
            await self.app(scope, receive, send)
            return
        if not self._gate.enter():
            response = JSONResponse(
                {"detail": "a backup restore is in progress; retry in a moment"}, status_code=503
            )
            await response(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self._gate.leave()
