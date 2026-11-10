"""`/api/backup/*`: export and restore, their security checks, and a restore under a live app."""

from __future__ import annotations

import base64
import io
import os
import tempfile
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.api.restore_gate import RestoreGate
from personal_finance_analyzer.backup import archive
from personal_finance_analyzer.backup.errors import BackupBusyError
from personal_finance_analyzer.db.models import Job, User
from tests.backup.support import PASSPHRASE, USER_NAME, seed_home

EXPORT = "/api/backup/export"
RESTORE = "/api/backup/restore"


@pytest.fixture
def other_machine_backup(tmp_path: Path) -> Path:
    """A passphrase-encrypted backup made from some other data directory."""
    other = tmp_path / "other_machine"
    seed_home(other)
    out = tmp_path / "from_other.pfabackup"
    archive.create_backup(out, passphrase=PASSPHRASE, home=other)
    return out


def _siblings(home: Path) -> list[str]:
    return sorted(p.name for p in home.parent.iterdir())


def _headers(passphrase: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/octet-stream"}
    if passphrase is not None:
        headers["X-Backup-Passphrase"] = base64.b64encode(passphrase.encode()).decode()
    return headers


def _upload(path: Path, passphrase: str | None = PASSPHRASE) -> dict[str, object]:
    """Keyword arguments for `client.post(RESTORE, ...)`: the archive as the raw body."""
    return {"content": path.read_bytes(), "headers": _headers(passphrase)}


def _scratch_leftovers(home: Path) -> list[str]:
    """Anything a restore or export left in the data directory's scratch area or beside it."""
    found = [p.name for p in home.parent.iterdir() if ".restore-" in p.name or "-upload-" in p.name]
    if (home / archive.WORK_DIRNAME).exists():
        found.append(archive.WORK_DIRNAME)
    return found


@pytest.fixture
def system_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty stand-in for the system temp directory: a backup operation must not write to it."""
    root = tmp_path / "system_tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


# ---- security ------------------------------------------------------------------------------


@pytest.mark.parametrize("route", [EXPORT, RESTORE])
def test_routes_require_token_and_host(client: TestClient, route: str) -> None:
    assert client.post(route, headers={"X-PFA-Token": ""}).status_code == 403
    assert client.post(route, headers={"X-PFA-Token": "wrong"}).status_code == 403
    assert client.post(route, headers={"Host": "evil.example"}).status_code == 403


def test_restore_upload_without_token_is_403_and_changes_nothing(
    client: TestClient, home: Path, other_machine_backup: Path
) -> None:
    before = (home / "finance.db").read_bytes()
    siblings = _siblings(home)
    response = client.post(
        RESTORE,
        content=other_machine_backup.read_bytes(),
        headers={**_headers(PASSPHRASE), "X-PFA-Token": ""},
    )
    assert response.status_code == 403
    assert (home / "finance.db").read_bytes() == before
    assert _siblings(home) == siblings


@pytest.mark.parametrize("route", [EXPORT, RESTORE])
def test_get_is_not_allowed(client: TestClient, route: str) -> None:
    assert client.get(route).status_code == 405


# ---- export --------------------------------------------------------------------------------


def test_export_encrypted_downloads_an_archive_restore_can_open(
    client: TestClient, home: Path, tmp_path: Path
) -> None:
    seed_home_over(home)
    response = client.post(EXPORT, json={"passphrase": PASSPHRASE})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["cache-control"] == "no-store"
    assert ".pfabackup" in response.headers["content-disposition"]
    assert not zipfile.is_zipfile(io.BytesIO(response.content))  # sealed, not a bare zip

    downloaded = tmp_path / "dl.pfabackup"
    downloaded.write_bytes(response.content)
    info = archive.inspect_backup(downloaded, passphrase=PASSPHRASE)
    assert info.encrypted
    assert info.manifest.counts["transactions"] == 1


def seed_home_over(home: Path) -> None:
    """Put a user and a transaction into the live database the test app already has open."""
    from personal_finance_analyzer.db.models import Account, Issuer, Statement, Transaction
    from personal_finance_analyzer.db.session import make_engine_for_path, make_session_factory

    engine = make_engine_for_path(home / "finance.db")
    try:
        with make_session_factory(engine)() as session:
            user = User(name=USER_NAME, is_default=True)
            issuer = Issuer(name="Fixture Bank", slug="fixture-bank")
            session.add_all([user, issuer])
            session.flush()
            account = Account(
                user_id=user.id, issuer_id=issuer.id, account_type="credit", display_name="Card"
            )
            session.add(account)
            session.flush()
            statement = Statement(
                account_id=account.id,
                user_id=user.id,
                file_sha256="a" * 64,
                original_name="s.pdf",
                status="parsed",
            )
            session.add(statement)
            session.flush()
            session.add(
                Transaction(
                    statement_id=statement.id,
                    account_id=account.id,
                    user_id=user.id,
                    posted_date="2026-01-02",
                    description_raw="raw",
                    description_clean="clean",
                    merchant_key="m",
                    amount_minor=100,
                    currency="USD",
                    kind="purchase",
                    is_spend=True,
                    dedupe_hash="h",
                )
            )
            session.commit()
    finally:
        engine.dispose()


def test_export_without_passphrase_is_refused_unless_opted_out(client: TestClient) -> None:
    refused = client.post(EXPORT, json={})
    assert refused.status_code == 400
    assert "passphrase is required" in refused.json()["detail"]

    short = client.post(EXPORT, json={"passphrase": "abc"})
    assert short.status_code == 400
    assert "at least" in short.json()["detail"]

    plain = client.post(EXPORT, json={"encrypt": False})
    assert plain.status_code == 200
    assert zipfile.is_zipfile(io.BytesIO(plain.content))


def test_export_rejects_unknown_fields(client: TestClient) -> None:
    assert (
        client.post(EXPORT, json={"passphrase": PASSPHRASE, "include_keys": True}).status_code
        == 422
    )


def test_export_keeps_plaintext_out_of_system_temp_and_cleans_up(
    client: TestClient, home: Path, system_tmp: Path
) -> None:
    assert client.post(EXPORT, json={"passphrase": PASSPHRASE}).status_code == 200
    assert client.post(EXPORT, json={"encrypt": False}).status_code == 200
    assert list(system_tmp.iterdir()) == []
    assert _scratch_leftovers(home) == []  # the response's background task removed the work dir


def test_failed_export_cleans_up_too(client: TestClient, home: Path, system_tmp: Path) -> None:
    (home / "config.toml").write_text("this is [not toml", encoding="utf-8")
    response = client.post(EXPORT, json={"passphrase": PASSPHRASE})
    assert response.status_code == 400
    assert list(system_tmp.iterdir()) == []
    assert _scratch_leftovers(home) == []


# ---- restore -------------------------------------------------------------------------------


def test_restore_replaces_data_and_the_running_app_serves_it(
    client: TestClient, app_instance: FastAPI, home: Path, other_machine_backup: Path
) -> None:
    assert USER_NAME not in [u["name"] for u in client.get("/api/users").json()]

    response = client.post(RESTORE, **_upload(other_machine_backup))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"]["transactions"] == 1
    assert body["migrated"] is False
    previous = Path(body["previous_dir"])
    assert previous.is_dir() and previous.name.endswith(".before-restore")
    assert (previous / "finance.db").is_file()  # the old data is kept, not deleted

    # Same app instance, same engine object: it now reads the restored database.
    names = [u["name"] for u in client.get("/api/users").json()]
    assert USER_NAME in names
    assert client.get("/api/statements").status_code == 200
    # ...and the gate is open again for ordinary traffic.
    assert app_instance.state.restore_gate.enter()
    app_instance.state.restore_gate.leave()


def test_restore_wrong_passphrase_is_400_and_touches_nothing(
    client: TestClient, home: Path, other_machine_backup: Path
) -> None:
    before = (home / "finance.db").read_bytes()
    siblings = _siblings(home)
    response = client.post(RESTORE, **_upload(other_machine_backup, "not the passphrase"))
    assert response.status_code == 400
    assert "wrong passphrase" in response.json()["detail"]
    missing = client.post(RESTORE, **_upload(other_machine_backup, None))
    assert missing.status_code == 400
    assert (home / "finance.db").read_bytes() == before
    assert _siblings(home) == siblings
    assert client.get("/api/users").status_code == 200  # still serving


def test_restore_rejects_junk_upload(client: TestClient, home: Path) -> None:
    siblings = _siblings(home)
    response = client.post(RESTORE, content=b"not a backup", headers=_headers(None))
    assert response.status_code == 400
    assert _siblings(home) == siblings


def test_restore_upload_over_the_cap_is_413(
    client: TestClient, home: Path, other_machine_backup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from personal_finance_analyzer.api.routers import backup as backup_router

    monkeypatch.setattr(backup_router, "MAX_BACKUP_UPLOAD_BYTES", 1024)
    siblings = _siblings(home)
    response = client.post(RESTORE, **_upload(other_machine_backup))
    assert response.status_code == 413
    assert _siblings(home) == siblings


def test_restore_streams_into_a_private_dir_never_system_temp(
    client: TestClient,
    home: Path,
    other_machine_backup: Path,
    system_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Path] = []
    real = archive.make_sibling_work_dir

    def spy(h: Path, prefix: str) -> Path:
        work = real(h, prefix)
        seen.append(work)
        if os.name != "nt":  # Windows has no POSIX permission bits (directories report 0o777)
            assert (work.stat().st_mode & 0o077) == 0  # owner only
        return work

    monkeypatch.setattr(archive, "make_sibling_work_dir", spy)
    response = client.post(RESTORE, **_upload(other_machine_backup))
    assert response.status_code == 200, response.text
    assert len(seen) == 1 and seen[0].parent == home.parent
    assert list(system_tmp.iterdir()) == []  # no spooled copy of the upload
    assert not seen[0].exists()
    previous = Path(response.json()["previous_dir"])
    assert not (previous / archive.WORK_DIRNAME).exists()  # no plaintext moved aside with it


def test_restore_upload_over_the_cap_is_cut_off_while_streaming(
    client: TestClient,
    home: Path,
    system_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Content-Length (a chunked body): the cap is enforced on the bytes actually received,
    the rest of the body is never read, and the partial file is deleted."""
    from personal_finance_analyzer.api.routers import backup as backup_router

    monkeypatch.setattr(backup_router, "MAX_BACKUP_UPLOAD_BYTES", 4096)
    sizes: list[int] = []
    real_stream = backup_router.stream_body_to_file

    async def watching(request: object, dest: Path, *, max_bytes: int) -> int:
        try:
            return await real_stream(request, dest, max_bytes=max_bytes)  # type: ignore[arg-type]
        finally:
            sizes.append(dest.stat().st_size)  # what reached the disk before the cut-off

    monkeypatch.setattr(backup_router, "stream_body_to_file", watching)

    def body() -> Iterator[bytes]:
        for _ in range(1000):
            yield b"x" * 1024

    siblings = _siblings(home)
    response = client.post(RESTORE, content=body(), headers=_headers(None))
    assert response.status_code == 413
    assert "cap" in response.json()["detail"]
    assert sizes and sizes[0] <= 4096  # never written past the cap
    assert _siblings(home) == siblings  # the partial upload's work dir is gone
    assert list(system_tmp.iterdir()) == []
    assert (home / "finance.db").is_file()
    assert client.get("/api/users").status_code == 200  # nothing was quiesced or swapped


def test_restore_announced_size_over_the_cap_is_refused_before_reading(
    client: TestClient, home: Path, other_machine_backup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from personal_finance_analyzer.api.routers import backup as backup_router

    monkeypatch.setattr(backup_router, "MAX_BACKUP_UPLOAD_BYTES", 10)
    response = client.post(RESTORE, **_upload(other_machine_backup))  # Content-Length > 10
    assert response.status_code == 413
    assert _scratch_leftovers(home) == []


def test_restore_wants_a_raw_body_not_multipart(client: TestClient, home: Path) -> None:
    siblings = _siblings(home)
    response = client.post(RESTORE, files={"file": ("x.pfabackup", b"data")})
    assert response.status_code == 415
    assert "raw body" in response.json()["detail"]
    assert _siblings(home) == siblings


def test_restore_passphrase_header_must_be_base64(client: TestClient, home: Path) -> None:
    headers = {"Content-Type": "application/octet-stream", "X-Backup-Passphrase": "not base64!!"}
    response = client.post(RESTORE, content=b"x", headers=headers)
    assert response.status_code == 400
    assert "base64" in response.json()["detail"]


def test_restore_passphrase_may_be_non_ascii(client: TestClient, tmp_path: Path) -> None:
    other = tmp_path / "unicode_machine"
    seed_home(other)
    sealed = tmp_path / "unicode.pfabackup"
    archive.create_backup(sealed, passphrase="pässwörd-日本語-ok", home=other)
    response = client.post(RESTORE, **_upload(sealed, "pässwörd-日本語-ok"))
    assert response.status_code == 200, response.text


def test_restore_refused_while_a_job_is_running(
    client: TestClient, session: Session, home: Path, other_machine_backup: Path
) -> None:
    session.add(Job(id="busy-1", kind="import", status="running"))
    session.commit()
    siblings = _siblings(home)

    response = client.post(RESTORE, **_upload(other_machine_backup))

    assert response.status_code == 409
    assert "job is still running" in response.json()["detail"]
    assert _siblings(home) == siblings
    assert _scratch_leftovers(home) == []
    # Nothing was swapped: the live database still has its running job, and the gate is released.
    assert [j.id for j in session.scalars(select(Job)).all()] == ["busy-1"]
    assert client.get("/api/users").status_code == 200


def test_restore_refused_when_server_uses_a_different_database(
    other_machine_backup: Path, tmp_path: Path, home: Path
) -> None:
    from personal_finance_analyzer.api.app import create_app
    from personal_finance_analyzer.config import Settings
    from personal_finance_analyzer.db.migrate import upgrade_head
    from personal_finance_analyzer.db.session import make_engine_for_path

    elsewhere = tmp_path / "elsewhere.db"
    upgrade_head(f"sqlite:///{elsewhere}")
    engine = make_engine_for_path(elsewhere)
    try:
        app = create_app(engine=engine, settings=Settings(), token="t")
        with TestClient(
            app, base_url="http://127.0.0.1", headers={"Host": "127.0.0.1", "X-PFA-Token": "t"}
        ) as other_client:
            response = other_client.post(RESTORE, **_upload(other_machine_backup))
        assert response.status_code == 409
        assert "not using the data directory" in response.json()["detail"]
    finally:
        engine.dispose()


def test_requests_get_503_while_a_restore_holds_the_gate(
    client: TestClient, app_instance: FastAPI
) -> None:
    gate: RestoreGate = app_instance.state.restore_gate
    gate.begin_exclusive()
    try:
        response = client.get("/api/users")
        assert response.status_code == 503
        assert "restore is in progress" in response.json()["detail"]
    finally:
        gate.end_exclusive()
    assert client.get("/api/users").status_code == 200


# ---- the gate itself -----------------------------------------------------------------------


def test_gate_waits_for_in_flight_requests_then_blocks_new_ones() -> None:
    gate = RestoreGate()
    assert gate.enter()  # the restore request itself
    assert gate.enter()  # another request already running
    finished = threading.Timer(0.1, gate.leave)
    finished.start()
    gate.begin_exclusive(timeout=5)  # returns once that other request leaves
    finished.join()
    assert not gate.enter()
    gate.end_exclusive()
    gate.leave()
    assert gate.enter()


def test_gate_times_out_when_requests_do_not_finish() -> None:
    gate = RestoreGate()
    gate.enter()
    gate.enter()  # never leaves
    with pytest.raises(BackupBusyError, match="busy"):
        gate.begin_exclusive(timeout=0.05)
    assert gate.enter()  # the gate reopened itself


def test_gate_rejects_a_second_exclusive() -> None:
    gate = RestoreGate()
    gate.enter()
    gate.begin_exclusive(timeout=1)
    with pytest.raises(BackupBusyError, match="already in progress"):
        gate.begin_exclusive(timeout=1)
    gate.end_exclusive()
