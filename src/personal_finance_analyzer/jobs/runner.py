"""One in-process worker thread consuming a queue of jobs, writing progress to the `jobs` table
(A1, D6). A single writer thread avoids SQLite write contention by construction; endpoints enqueue
a callable and return the `job_id` immediately.

Each queued task is a zero-argument callable that does its own DB work with its own `Session`
(from the shared `session_factory`) and reports progress through the `JobProgress` handed to it,
which updates the `jobs` row and commits after every update so progress survives a page reload
and two concurrent runs never corrupt each other's row (each has its own `job_id`/row).
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from personal_finance_analyzer.core.logging import get_logger
from personal_finance_analyzer.db.models import Job, utcnow_iso

logger = get_logger("jobs.runner")

#: A background task: given a `JobProgress` bound to its own job row, do the work and return a
#: JSON-serializable result (or `None`).
JobTask = Callable[["JobProgress"], dict[str, Any] | None]


@dataclass
class JobProgress:
    """Handed to a queued task; every call persists to the `jobs` row and commits."""

    job_id: str
    _session_factory: sessionmaker[Session]

    def update(self, done: int, total: int, cost_usd: float = 0.0) -> None:
        """Report progress. `done`/`total` must be monotonically non-decreasing across calls for
        one job (the SSE stream and the UI both assume this)."""
        with self._session_factory() as session:
            job = session.get(Job, self.job_id)
            if job is None:  # pragma: no cover - defensive; job rows are never deleted
                return
            job.status = "running"
            job.done = done
            job.total = total
            job.cost_usd = cost_usd
            job.updated_at = utcnow_iso()
            session.commit()


class JobRunner:
    """A single background worker thread draining an in-process queue of `JobTask`s."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._queue: queue.Queue[tuple[str, JobTask] | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the worker thread. Safe to call once; a second call is a no-op."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="pfa-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the worker thread to exit after draining any already-queued tasks."""
        if self._thread is None:
            return
        self._queue.put(None)
        self._thread.join(timeout=5)
        self._thread = None

    def enqueue(self, *, kind: str, payload: dict[str, Any] | None = None) -> str:
        """Create a `queued` `jobs` row and return its id. Call `submit()` with the same id to
        hand the runner the callable that does the work, once the caller has finished building
        it (letting a route build a task that closes over the job id itself, e.g. for
        `progress_cb`)."""
        job_id = str(uuid.uuid4())
        with self._session_factory() as session:
            session.add(
                Job(
                    id=job_id,
                    kind=kind,
                    status="queued",
                    payload_json=json.dumps(payload or {}),
                )
            )
            session.commit()
        return job_id

    def submit(self, job_id: str, task: JobTask) -> None:
        """Hand the runner the callable for a job created by `enqueue`."""
        self._queue.put((job_id, task))

    def run_job(self, *, kind: str, task: JobTask, payload: dict[str, Any] | None = None) -> str:
        """Convenience: `enqueue` then `submit` in one call. Returns the `job_id`."""
        job_id = self.enqueue(kind=kind, payload=payload)
        self.submit(job_id, task)
        return job_id

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job_id, task = item
            self._execute(job_id, task)

    def _execute(self, job_id: str, task: JobTask) -> None:
        progress = JobProgress(job_id=job_id, _session_factory=self._session_factory)
        try:
            result = task(progress)
        except Exception as exc:  # a broken job must not kill the worker thread
            logger.warning("job %s failed", job_id, exc_info=True)
            with self._session_factory() as session:
                job = session.get(Job, job_id)
                if job is not None:
                    job.status = "error"
                    job.error_detail = str(exc)
                    job.updated_at = utcnow_iso()
                    session.commit()
            return
        with self._session_factory() as session:
            job = session.get(Job, job_id)
            if job is not None:
                job.status = "done"
                job.done = job.total = max(job.total, job.done, 1)
                job.result_json = json.dumps(result or {})
                job.updated_at = utcnow_iso()
                session.commit()
