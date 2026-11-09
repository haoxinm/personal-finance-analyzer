"""`/api/watch/*` and the `[watch]` part of `/api/settings`."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from personal_finance_analyzer.config import load_file_settings, load_settings
from personal_finance_analyzer.core.paths import documents_dir
from personal_finance_analyzer.db.models import User
from personal_finance_analyzer.watch.scanner import FolderScanner
from personal_finance_analyzer.watch.service import WatchService
from tests.watch.support import add_bank, add_person, put_pdf_bytes, put_statement, put_tax

STATEMENT = "Alex/Statements/Fixture Bank/jan.pdf"


def _watch(app: FastAPI) -> WatchService:
    service: WatchService = app.state.watch
    return service


def _no_wait(app: FastAPI) -> None:
    """Scans in these tests treat new files as settled at once (no real pause)."""
    scanner: FolderScanner = _watch(app).scanner
    scanner._sleep = lambda _s: None


def _scan(client: TestClient, app: FastAPI) -> dict[str, object]:
    response = client.post("/api/watch/scan")
    assert response.status_code == 202
    assert _watch(app).wait_idle(30)
    status: dict[str, object] = client.get("/api/watch/status").json()
    return status


@pytest.fixture
def alex(session: Session) -> User:
    add_bank(session)
    return add_person(session, "Alex", default=True)


def test_status_when_nothing_has_run(client: TestClient) -> None:
    body = client.get("/api/watch/status").json()
    assert body["enabled"] is False
    assert body["interval_seconds"] == 60
    assert body["documents_dir"] == str(documents_dir())
    assert body["running"] is False and body["last_scan"] is None and body["next_scan_at"] is None


def test_scan_now_imports_and_the_status_and_lists_report_it(
    client: TestClient, app_instance: FastAPI, alex: User, session: Session
) -> None:
    _no_wait(app_instance)
    put_statement(STATEMENT)
    put_tax("Alex/Tax/2024/W-2.pdf")
    put_statement("Taylor/Statements/jan.pdf", "fx")
    started = client.post("/api/watch/scan").json()
    assert started["started"] is True
    assert _watch(app_instance).wait_idle(30)
    status = client.get("/api/watch/status").json()
    last = status["last_scan"]
    assert (last["imported"], last["failed"], last["scanned"]) == (2, 1, 3)
    assert status["running"] is False and status["problem"] is None

    recent = client.get("/api/watch/files").json()
    assert recent["total"] == 2
    kinds = {f["path"]: f["kind"] for f in recent["items"]}
    assert kinds == {STATEMENT: "statement", "Alex/Tax/2024/W-2.pdf": "tax"}
    statement_row = next(f for f in recent["items"] if f["kind"] == "statement")
    assert statement_row["statement_id"] is not None and statement_row["outcome"] == "imported"
    tax_row = next(f for f in recent["items"] if f["kind"] == "tax")
    assert tax_row["tax_document_id"] is not None

    problems = client.get("/api/watch/files", params={"view": "problems"}).json()
    assert problems["total"] == 1
    (problem,) = problems["items"]
    assert problem["path"] == "Taylor/Statements/jan.pdf"
    assert problem["code"] == "unknown_person" and problem["folder_person"] == "Taylor"
    assert "Taylor" in problem["hint"]


def test_retry_clears_a_problem_and_the_next_scan_tries_again(
    client: TestClient, app_instance: FastAPI, alex: User, session: Session
) -> None:
    _no_wait(app_instance)
    put_pdf_bytes("Alex/Statements/Bank/broken.pdf", b"%PDF-1.4\nnot a pdf\n%%EOF\n")
    _scan(client, app_instance)
    (problem,) = client.get("/api/watch/files", params={"view": "problems"}).json()["items"]
    first_seen = problem["last_seen"]
    assert client.post(f"/api/watch/files/{problem['id']}/retry").status_code == 204
    assert client.get("/api/watch/files", params={"view": "problems"}).json()["total"] == 0
    time.sleep(0.01)
    _scan(client, app_instance)
    (again,) = client.get("/api/watch/files", params={"view": "problems"}).json()["items"]
    assert again["path"] == problem["path"] and again["last_seen"] > first_seen


def test_retry_of_something_that_is_not_a_problem_is_404(
    client: TestClient, app_instance: FastAPI, alex: User
) -> None:
    _no_wait(app_instance)
    put_statement(STATEMENT)
    _scan(client, app_instance)
    (done,) = client.get("/api/watch/files").json()["items"]
    assert client.post(f"/api/watch/files/{done['id']}/retry").status_code == 404
    assert client.post("/api/watch/files/9999/retry").status_code == 404


def test_the_file_list_is_limited_and_validated(client: TestClient) -> None:
    assert client.get("/api/watch/files", params={"limit": 0}).status_code == 422
    assert client.get("/api/watch/files", params={"view": "nope"}).status_code == 422
    assert client.get("/api/watch/files", params={"limit": 1}).json() == {"items": [], "total": 0}


def test_a_second_scan_request_while_one_runs_does_not_start_another(
    client: TestClient, app_instance: FastAPI
) -> None:
    service = _watch(app_instance)
    with service._lock:
        service._running = True
    try:
        body = client.post("/api/watch/scan").json()
        assert body["started"] is False and body["status"]["running"] is True
    finally:
        with service._lock:
            service._running = False


def test_the_watch_endpoints_need_the_api_token(app_instance: FastAPI) -> None:
    with TestClient(
        app_instance, base_url="http://127.0.0.1", headers={"Host": "127.0.0.1"}
    ) as bare:
        assert bare.get("/api/watch/status").status_code in (401, 403)
        assert bare.post("/api/watch/scan").status_code in (401, 403)


# ----------------------------------------------------------------------------- settings


def test_settings_carry_the_watch_section_and_the_folder(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    assert body["watch"] == {
        "enabled": False,
        "interval_seconds": 60,
        "watched_folder": str(documents_dir()),
    }


def test_the_watch_settings_round_trip(client: TestClient, app_instance: FastAPI) -> None:
    body = client.get("/api/settings").json()
    body["watch"] = {"enabled": True, "interval_seconds": 300}
    saved = client.put("/api/settings", json=body).json()
    assert saved["watch"]["enabled"] is True and saved["watch"]["interval_seconds"] == 300
    watch = load_file_settings().watch
    assert (watch.enabled, watch.interval_seconds) == (True, 300)
    assert client.get("/api/watch/status").json()["enabled"] is True


def test_the_interval_is_clamped_by_the_api(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    body["watch"] = {"enabled": False, "interval_seconds": 2}
    assert client.put("/api/settings", json=body).json()["watch"]["interval_seconds"] == 10


def test_an_older_client_that_omits_watch_keeps_the_saved_value(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    body["watch"] = {"enabled": True, "interval_seconds": 120}
    client.put("/api/settings", json=body)
    older = client.get("/api/settings").json()
    del older["watch"]
    assert client.put("/api/settings", json=older).status_code == 200
    assert load_settings().watch.enabled is True and load_settings().watch.interval_seconds == 120


def test_an_environment_override_is_reported_and_never_written_to_the_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PFA_WATCH_ENABLED", "true")
    body = client.get("/api/settings").json()
    assert body["watch"]["enabled"] is True and "watch.enabled" in body["env_overrides"]
    body["watch"]["enabled"] = False  # the UI cannot change it...
    saved = client.put("/api/settings", json=body).json()
    assert saved["watch"]["enabled"] is True  # ...the environment still decides,
    monkeypatch.delenv("PFA_WATCH_ENABLED")
    assert load_file_settings().watch.enabled is False  # ...and the file kept its own value


def test_the_service_starts_and_stops_with_the_app(app_instance: FastAPI) -> None:
    service = _watch(app_instance)
    assert service._thread is None  # create_app alone starts nothing
    with TestClient(app_instance, base_url="http://127.0.0.1"):
        assert service._thread is not None
    assert service._thread is None
