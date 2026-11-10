"""`/api/jobs/{id}` and the SSE stream (A1, D6). Also covers "two concurrent classify requests do
not corrupt job state" (each request gets its own job row; the runner drains its queue in order)."""

from __future__ import annotations

import json
import threading
import time

from fastapi.testclient import TestClient

from personal_finance_analyzer.jobs.runner import JobProgress, JobRunner


def test_get_unknown_job_is_404(client: TestClient) -> None:
    response = client.get("/api/jobs/does-not-exist")
    assert response.status_code == 404


def test_job_runner_reports_progress_and_completes() -> None:
    import tempfile
    from pathlib import Path

    from personal_finance_analyzer.db.migrate import upgrade_head
    from personal_finance_analyzer.db.session import make_engine_for_path, make_session_factory

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "finance.db"
        upgrade_head(f"sqlite:///{db_path}")
        engine = make_engine_for_path(db_path)
        session_factory = make_session_factory(engine)
        runner = JobRunner(session_factory)
        runner.start()

        def task(progress: JobProgress) -> dict[str, object]:
            for i in range(1, 4):
                progress.update(i, 3)
                time.sleep(0.01)
            return {"ok": True}

        job_id = runner.run_job(kind="import", task=task)

        deadline = time.time() + 5
        with session_factory() as session:
            from personal_finance_analyzer.db.models import Job

            job = session.get(Job, job_id)
            while job is not None and job.status != "done" and time.time() < deadline:
                session.expire(job)
                job = session.get(Job, job_id)
                time.sleep(0.02)
            assert job is not None
            assert job.status == "done"
            assert job.done == job.total == 3
            assert json.loads(job.result_json or "{}") == {"ok": True}
        runner.stop()
        engine.dispose()


def test_job_runner_records_task_errors() -> None:
    import tempfile
    from pathlib import Path

    from personal_finance_analyzer.db.migrate import upgrade_head
    from personal_finance_analyzer.db.models import Job
    from personal_finance_analyzer.db.session import make_engine_for_path, make_session_factory

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "finance.db"
        upgrade_head(f"sqlite:///{db_path}")
        engine = make_engine_for_path(db_path)
        session_factory = make_session_factory(engine)
        runner = JobRunner(session_factory)
        runner.start()

        def failing_task(progress: JobProgress) -> None:
            raise RuntimeError("boom")

        job_id = runner.run_job(kind="classify", task=failing_task)

        deadline = time.time() + 5
        with session_factory() as session:
            job = session.get(Job, job_id)
            while (
                job is not None and job.status not in ("done", "error") and time.time() < deadline
            ):
                session.expire(job)
                job = session.get(Job, job_id)
                time.sleep(0.02)
            assert job is not None
            assert job.status == "error"
            assert job.error_detail is not None and "boom" in job.error_detail
        runner.stop()
        engine.dispose()


def test_sse_stream_reports_progress_and_terminates(app_instance, client: TestClient) -> None:
    runner: JobRunner = app_instance.state.job_runner

    def task(progress: JobProgress) -> dict[str, object]:
        progress.update(1, 2)
        time.sleep(0.05)
        progress.update(2, 2)
        return {}

    job_id = runner.run_job(kind="import", task=task)

    with client.stream("GET", f"/api/jobs/{job_id}/events?token=test-token") as response:
        assert response.status_code == 200
        progresses: list[float] = []
        for line in response.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            payload = json.loads(line[len("data: ") :])
            progresses.append(payload["progress"])
            if payload["status"] in ("done", "error"):
                break

    assert progresses == sorted(progresses)
    assert progresses[-1] == 1.0


def test_two_concurrent_classify_jobs_do_not_corrupt_state(app_instance) -> None:
    runner: JobRunner = app_instance.state.job_runner
    results: list[str] = []
    lock = threading.Lock()

    def make_task(label: str):
        def task(progress: JobProgress) -> dict[str, object]:
            progress.update(1, 1)
            with lock:
                results.append(label)
            return {"label": label}

        return task

    job_a = runner.run_job(kind="classify", task=make_task("a"))
    job_b = runner.run_job(kind="classify", task=make_task("b"))

    from personal_finance_analyzer.db.models import Job

    deadline = time.time() + 5
    session_factory = app_instance.state.session_factory
    with session_factory() as session:
        while time.time() < deadline:
            a = session.get(Job, job_a)
            b = session.get(Job, job_b)
            session.expire_all()
            if a is not None and b is not None and a.status == "done" and b.status == "done":
                break
            time.sleep(0.02)
        assert a is not None and b is not None
        assert a.status == "done" and b.status == "done"
        assert json.loads(a.result_json or "{}") == {"label": "a"}
        assert json.loads(b.result_json or "{}") == {"label": "b"}

    assert sorted(results) == ["a", "b"]
