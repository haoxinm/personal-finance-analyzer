"""A backup archive is hostile input and its plaintext intermediates are sensitive: extraction
limits, the private scratch directory (and sweeping what a crash leaves in it), the scrypt memory
cap, and the "database in use" check."""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import struct
import time
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest

from personal_finance_analyzer.backup import archive, crypto
from personal_finance_analyzer.backup.errors import (
    BackupError,
    BackupFormatError,
    RestoreTargetError,
)
from personal_finance_analyzer.db.migrate import upgrade_head

from .support import PASSPHRASE, seed_home

_HOUR = 3600


def _target(tmp_path: Path) -> Path:
    return tmp_path / "machine_b" / "PersonalFinanceAnalyzer"


def _backup(tmp_path: Path) -> Path:
    out = tmp_path / "good.pfabackup"
    archive.create_backup(out, passphrase=None)
    return out


def _with_manifest(src: Path, dest: Path, edit: object) -> None:
    """Copy zip ``src`` to ``dest`` with its manifest passed through ``edit(manifest_dict)``."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name == archive.MANIFEST_NAME:
                manifest = json.loads(data)
                edit(manifest)  # type: ignore[operator]
                data = json.dumps(manifest).encode()
            zout.writestr(name, data)


# ---- extraction limits ---------------------------------------------------------------------


def test_manifest_declaring_a_huge_member_is_refused_before_anything_is_written(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    def declare_huge(manifest: dict[str, object]) -> None:
        for entry in manifest["files"]:  # type: ignore[attr-defined]
            if entry["path"] == archive.DB_MEMBER:
                entry["size"] = 10**12

    crafted = tmp_path / "huge.pfabackup"
    _with_manifest(_backup(tmp_path), crafted, declare_huge)
    target = _target(tmp_path)
    with pytest.raises(BackupFormatError, match="larger than a file in a backup may be"):
        archive.restore_backup(crafted, passphrase=None, home=target)
    assert not target.exists()
    assert [p.name for p in target.parent.iterdir()] == []  # no staging directory left


def test_declared_sizes_that_add_up_past_the_total_are_refused(
    source: tuple[Path, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 2000)  # the fixture's db alone is far larger
    target = _target(tmp_path)
    with pytest.raises(BackupFormatError, match="add up to more than"):
        archive.restore_backup(_backup(tmp_path), passphrase=None, home=target)
    assert not target.exists()


def test_member_that_inflates_past_its_declared_size_is_stopped_at_the_declared_size(
    source: tuple[Path, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zip itself says the member is big (a highly compressible bomb), the manifest says it is
    small: bytes are counted as they are written, so the extraction stops right at the declared
    size instead of inflating the member."""
    good = _backup(tmp_path)

    def shrink(manifest: dict[str, object]) -> None:
        for entry in manifest["files"]:  # type: ignore[attr-defined]
            if entry["path"] == "specs/my-bank.yaml":
                entry["size"] = 10

    bomb = tmp_path / "bomb.pfabackup"
    with zipfile.ZipFile(good) as zin, zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name == "specs/my-bank.yaml":
                data = b"0" * (50 * 1024 * 1024)  # 50 MiB of zeros, a few KiB compressed
            elif name == archive.MANIFEST_NAME:
                manifest = json.loads(data)
                shrink(manifest)
                data = json.dumps(manifest).encode()
            zout.writestr(name, data)
    assert bomb.stat().st_size < 1024 * 1024

    written: list[int] = []
    real = archive._copy_hashed

    def counting(src: object, dst: io.BufferedWriter, **kw: object) -> tuple[int, str]:
        try:
            return real(src, dst, **kw)  # type: ignore[arg-type]
        finally:
            dst.flush()
            written.append(os.path.getsize(dst.name))

    monkeypatch.setattr(archive, "_copy_hashed", counting)
    with pytest.raises(BackupFormatError, match="larger than the backup's manifest"):
        archive.restore_backup(bomb, passphrase=None, home=_target(tmp_path))
    assert max(written) <= archive._COPY_CHUNK  # nowhere near 50 MiB hit the disk


def test_too_many_members_are_refused(
    source: tuple[Path, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _backup(tmp_path)
    crowded = tmp_path / "crowded.pfabackup"
    with zipfile.ZipFile(good) as zin, zipfile.ZipFile(crowded, "w") as zout:
        for name in zin.namelist():
            zout.writestr(name, zin.read(name))
        for i in range(30):
            zout.writestr(f"specs/extra-{i}.yaml", b"x: 1")
    monkeypatch.setattr(archive, "MAX_MEMBERS", 20)
    with pytest.raises(BackupFormatError, match="more than a backup may contain"):
        archive.inspect_backup(crowded)
    with pytest.raises(BackupFormatError, match="more than a backup may contain"):
        archive.restore_backup(crowded, passphrase=None, home=_target(tmp_path))


def test_per_kind_limits_apply() -> None:
    assert archive._member_limit(archive.DB_MEMBER) == archive.MAX_DB_BYTES
    assert archive._member_limit("statements/" + "a" * 64 + ".pdf") == archive.MAX_PDF_BYTES
    assert archive._member_limit("config.toml") == archive.MAX_TEXT_BYTES
    assert archive.MAX_PDF_BYTES < archive.MAX_DB_BYTES < archive.MAX_TOTAL_BYTES


def test_archive_that_cannot_fit_on_the_disk_is_refused(
    source: tuple[Path, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _backup(tmp_path)
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: usage._replace(free=1000))
    target = _target(tmp_path)
    with pytest.raises(BackupError, match="not enough free disk space"):
        archive.restore_backup(good, passphrase=None, home=target)
    assert not target.exists()


def test_write_failure_while_extracting_is_a_clean_error(
    source: tuple[Path, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _backup(tmp_path)

    def disk_full(*_a: object, **_k: object) -> tuple[int, str]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(archive, "_copy_hashed", disk_full)
    target = _target(tmp_path)
    with pytest.raises(BackupError, match="disk full"):
        archive.restore_backup(good, passphrase=None, home=target)
    assert [p.name for p in target.parent.iterdir()] == []


# ---- scratch directory ---------------------------------------------------------------------


def test_export_plaintext_zip_stays_inside_the_data_directory(
    source: tuple[Path, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _ = source
    system_tmp = tmp_path / "system_tmp"
    system_tmp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(system_tmp))
    seen: list[Path] = []
    real_encrypt = crypto.encrypt_stream

    def spy(src: io.BufferedReader, dst: io.BufferedWriter, *args: object, **kw: object) -> None:
        seen.append(Path(src.name))
        real_encrypt(src, dst, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(crypto, "encrypt_stream", spy)
    archive.create_backup(tmp_path / "b.pfabackup", passphrase=PASSPHRASE)

    assert len(seen) == 1
    assert home in seen[0].parents
    assert list(system_tmp.iterdir()) == []  # nothing in the system temp directory
    assert not (home / archive.WORK_DIRNAME).exists()  # cleaned up, and the parent with it


def test_inspect_and_restore_use_no_system_temp(
    source: tuple[Path, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed = tmp_path / "sealed.pfabackup"
    archive.create_backup(sealed, passphrase=PASSPHRASE)
    system_tmp = tmp_path / "system_tmp"
    system_tmp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(system_tmp))
    archive.inspect_backup(sealed, passphrase=PASSPHRASE)
    archive.restore_backup(sealed, passphrase=PASSPHRASE, home=_target(tmp_path))
    assert list(system_tmp.iterdir()) == []


def test_work_dir_is_private_and_cleaned_up_on_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(RuntimeError), archive.work_dir(home, "x") as work:
        assert work.parent == home / archive.WORK_DIRNAME
        if os.name != "nt":  # Windows has no POSIX permission bits (directories report 0o777)
            assert (work.parent.stat().st_mode & 0o077) == 0
        (work / "plain.zip").write_bytes(b"data")
        raise RuntimeError("boom")
    assert not (home / archive.WORK_DIRNAME).exists()


def test_work_dir_for_a_missing_home_creates_nothing_inside_it(tmp_path: Path) -> None:
    home = tmp_path / "fresh" / "PersonalFinanceAnalyzer"
    with archive.work_dir(home, "inspect") as work:
        assert work.parent == home.parent
    assert not home.exists()
    assert list(home.parent.iterdir()) == []


def test_sibling_work_dir_is_private_and_beside_the_home(tmp_path: Path) -> None:
    home = tmp_path / "PersonalFinanceAnalyzer"
    home.mkdir()
    work = archive.make_sibling_work_dir(home, "restore-upload")
    try:
        assert work.parent == tmp_path
        if os.name != "nt":  # Windows has no POSIX permission bits (directories report 0o777)
            assert (work.stat().st_mode & 0o077) == 0
        assert not any(home.iterdir())  # nothing inside the directory a restore moves aside
    finally:
        archive.remove_work_dir(work)
    assert not work.exists()


# ---- sweeping what a crash left behind -----------------------------------------------------


def _age(path: Path, hours: float) -> None:
    stamp = time.time() - hours * _HOUR
    for p in [path, *path.rglob("*")]:
        os.utime(p, (stamp, stamp))


def test_sweep_removes_old_work_dirs_and_keeps_fresh_ones(tmp_path: Path) -> None:
    home = tmp_path / "PersonalFinanceAnalyzer"
    home.mkdir()
    base = home / archive.WORK_DIRNAME
    old_export, fresh_export = base / "export-old", base / "export-new"
    for d in (old_export, fresh_export):
        d.mkdir(parents=True)
        (d / "plain.zip").write_bytes(b"PK plaintext")
    _age(old_export, 30)

    removed = archive.sweep_stale_work_dirs(home)

    assert removed == [old_export]
    assert not old_export.exists()
    assert (fresh_export / "plain.zip").exists()
    assert base.exists()  # not empty: kept


def test_sweep_removes_the_scratch_parent_when_it_empties(tmp_path: Path) -> None:
    home = tmp_path / "PersonalFinanceAnalyzer"
    (home / archive.WORK_DIRNAME / "backup-aaaa").mkdir(parents=True)
    _age(home / archive.WORK_DIRNAME / "backup-aaaa", 48)
    archive.sweep_stale_work_dirs(home)
    assert not (home / archive.WORK_DIRNAME).exists()


def test_sweep_uses_the_newest_file_so_a_slow_upload_is_not_swept(tmp_path: Path) -> None:
    home = tmp_path / "PersonalFinanceAnalyzer"
    work = home / archive.WORK_DIRNAME / "restore-live"
    work.mkdir(parents=True)
    (work / "upload.part").write_bytes(b"still being written")
    os.utime(work, (time.time() - 40 * _HOUR,) * 2)  # the directory itself looks old
    assert archive.sweep_stale_work_dirs(home) == []
    assert (work / "upload.part").exists()


def test_sweep_removes_stale_sibling_staging_dirs_but_only_exact_matches(tmp_path: Path) -> None:
    home = tmp_path / "PersonalFinanceAnalyzer"
    home.mkdir()
    stale_stage = (
        tmp_path / ".PersonalFinanceAnalyzer.restore-ab12cd34"
    )  # a killed restore's staging dir
    stale_upload = tmp_path / ".PersonalFinanceAnalyzer.restore-upload-xy98zw76"
    fresh_stage = tmp_path / ".PersonalFinanceAnalyzer.restore-fresh123"
    for d in (stale_stage, stale_upload, fresh_stage):
        (d / "work").mkdir(parents=True)
        (d / "work" / "archive.zip").write_bytes(b"plaintext")
    # Things that merely look similar must never be touched.
    keep = [
        tmp_path / "PersonalFinanceAnalyzer.20260101T000000.before-restore",
        tmp_path / ".PersonalFinanceAnalyzer.restore-short",
        tmp_path / ".PersonalFinanceAnalyzer.restore-ab12cd34.bak",
        tmp_path / ".OtherApp.restore-ab12cd34",
        tmp_path / "notes",
    ]
    for d in keep:
        d.mkdir()
    for d in (stale_stage, stale_upload, *keep):
        _age(d, 100)

    removed = set(archive.sweep_stale_work_dirs(home))

    assert removed == {stale_stage, stale_upload}
    assert fresh_stage.exists()
    assert all(d.exists() for d in keep)
    assert home.exists()


def test_sweep_honours_max_age_and_a_fixed_clock(tmp_path: Path) -> None:
    home = tmp_path / "PersonalFinanceAnalyzer"
    work = home / archive.WORK_DIRNAME / "export-1"
    work.mkdir(parents=True)
    assert archive.sweep_stale_work_dirs(home) == []
    removed = archive.sweep_stale_work_dirs(
        home, max_age=timedelta(hours=1), now=time.time() + 2 * _HOUR
    )
    assert removed == [work]


def test_next_backup_operation_sweeps_a_crashed_ones_leftovers(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    home, _ = source
    leftover = home / archive.WORK_DIRNAME / "restore-crashed"
    leftover.mkdir(parents=True)
    (leftover / "archive.zip").write_bytes(b"PK plaintext of a killed restore")
    _age(leftover, 24)
    stage = tmp_path / f".{home.name}.restore-deadbeef"
    (stage / "work").mkdir(parents=True)
    (stage / "work" / "archive.zip").write_bytes(b"PK plaintext")
    _age(stage, 24)

    archive.create_backup(tmp_path / "b.pfabackup", passphrase=None)

    assert not leftover.exists()
    assert not stage.exists()


def test_restore_sweeps_a_crashed_restores_staging_dir(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    good = _backup(tmp_path)
    target = _target(tmp_path)
    stale = target.parent / f".{target.name}.restore-deadbeef"
    (stale / "work").mkdir(parents=True)
    (stale / "work" / "archive.zip").write_bytes(b"PK plaintext")
    _age(stale, 24)
    archive.restore_backup(good, passphrase=None, home=target)
    assert not stale.exists()
    assert (target / "finance.db").is_file()


def test_app_start_sweeps_stale_work_dirs(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    from personal_finance_analyzer.api.app import create_app
    from personal_finance_analyzer.config import Settings
    from personal_finance_analyzer.db.session import make_engine_for_path

    home, _ = source
    leftover = home / archive.WORK_DIRNAME / "export-crashed"
    leftover.mkdir(parents=True)
    (leftover / "x.pfabackup").write_bytes(b"PK")
    _age(leftover, 24)
    engine = make_engine_for_path(home / "finance.db")
    try:
        create_app(engine=engine, settings=Settings(), token="t", web_dir=tmp_path / "no-web")
    finally:
        engine.dispose()
    assert not leftover.exists()


# ---- scrypt memory cap ---------------------------------------------------------------------


def test_header_asking_for_more_than_1_gib_of_scrypt_memory_is_rejected() -> None:
    # N = 2**20, r = 16 is 2 GiB: inside each per-field bound, over the memory cap.
    header = struct.pack(">8sBBBI16s7s", crypto.MAGIC, 20, 16, 1, 65536, b"s" * 16, b"p" * 7)
    with pytest.raises(BackupFormatError, match="memory"):
        crypto.decrypt_stream(io.BytesIO(header + b"x" * 32), io.BytesIO(), "whatever-pass")


# ---- "database in use" ---------------------------------------------------------------------


def _rollback_db(path: Path) -> None:
    upgrade_head(f"sqlite:///{path}")
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()


def test_idle_database_is_not_in_use(tmp_path: Path) -> None:
    db = tmp_path / "finance.db"
    _rollback_db(db)
    assert archive._db_in_use(db) is False
    assert archive._db_in_use(tmp_path / "missing.db") is False


def test_rollback_journal_database_in_a_transaction_is_in_use(tmp_path: Path) -> None:
    """No `-wal`/`-shm` sidecars exist in rollback-journal mode, so the old check saw nothing; the
    lock probe sees another process in the middle of a transaction."""
    db = tmp_path / "finance.db"
    _rollback_db(db)
    server = sqlite3.connect(db, isolation_level=None)
    try:
        server.execute("BEGIN EXCLUSIVE")
        assert not any(p.name.endswith(("-wal", "-shm")) for p in tmp_path.iterdir())
        assert archive._db_in_use(db) is True
        server.execute("ROLLBACK")
        assert archive._db_in_use(db) is False
    finally:
        server.close()


def test_rollback_journal_database_being_read_is_in_use(tmp_path: Path) -> None:
    db = tmp_path / "finance.db"
    _rollback_db(db)
    reader = sqlite3.connect(db, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM users").fetchone()  # holds a SHARED lock
        assert archive._db_in_use(db) is True
    finally:
        reader.close()
    assert archive._db_in_use(db) is False


def test_restore_refuses_a_database_locked_in_rollback_mode(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    good = _backup(tmp_path)
    target = _target(tmp_path)
    seed_home(target)
    _rollback_db_in_place = sqlite3.connect(target / "finance.db")
    _rollback_db_in_place.execute("PRAGMA journal_mode=DELETE")
    _rollback_db_in_place.close()
    server = sqlite3.connect(target / "finance.db", isolation_level=None)
    server.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(RestoreTargetError, match="open in another process"):
            archive.restore_backup(good, passphrase=None, replace=True, home=target)
        assert [p.name for p in target.parent.iterdir()] == [target.name]
    finally:
        server.execute("ROLLBACK")
        server.close()
    archive.restore_backup(good, passphrase=None, replace=True, home=target)
