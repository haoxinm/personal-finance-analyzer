from __future__ import annotations

import io
import json
import sqlite3
import tomllib
import zipfile
from pathlib import Path

import pytest
from alembic import command

from personal_finance_analyzer import config as config_module
from personal_finance_analyzer.backup import archive, crypto
from personal_finance_analyzer.backup.errors import (
    BackupError,
    BackupFormatError,
    PassphraseError,
    RestoreTargetError,
    SchemaTooNewError,
)
from personal_finance_analyzer.db.migrate import build_alembic_config

from .support import (
    CONFIG_TOML,
    DESCRIPTION,
    PASSPHRASE,
    SECRET_API_KEY,
    SECRET_HEADER,
    SECRET_URL_PASSWORD,
    USER_NAME,
    seed_home,
    table_rows,
    tree,
)

_ALL_TABLES = (
    "users",
    "issuers",
    "accounts",
    "statements",
    "transactions",
    "categories",
    "subcategories",
    "tax_documents",
    "tax_forms",
    "balance_entries",
    "rules",
    "layout_specs",
    "alembic_version",
)


def _target(tmp_path: Path) -> Path:
    return tmp_path / "machine_b" / "PersonalFinanceAnalyzer"


def _rewrite_zip(src: Path, dest: Path, change: dict[str, bytes | None]) -> None:
    """Copy zip ``src`` to ``dest`` replacing (or, with ``None``, dropping) members, adding new
    ones, and leaving the manifest alone unless it is in ``change``."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w") as zout:
        for name in zin.namelist():
            if name in change:
                if change[name] is not None:
                    zout.writestr(name, change[name] or b"")
            else:
                zout.writestr(name, zin.read(name))
        for name, data in change.items():
            if name not in zin.namelist() and data is not None:
                zout.writestr(name, data)


def _manifest_of(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as zf:
        manifest: dict[str, object] = json.loads(zf.read(archive.MANIFEST_NAME))
        return manifest


def table_columns(db_file: Path, table: str) -> list[str]:
    con = sqlite3.connect(db_file)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def test_round_trip_reproduces_rows_and_files(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    home_a, shas = source
    backup = tmp_path / "all.pfabackup"
    manifest = archive.create_backup(backup, passphrase=PASSPHRASE)
    assert manifest.counts == {
        "users": 1,
        "accounts": 1,
        "statements": 1,
        "transactions": 1,
        "tax_documents": 1,
    }
    assert crypto.is_encrypted(backup.open("rb"))

    home_b = _target(tmp_path)
    result = archive.restore_backup(backup, passphrase=PASSPHRASE, home=home_b)
    assert result.previous_dir is None
    assert not result.migrated

    for table in _ALL_TABLES:
        assert table_rows(home_b / "finance.db", table, skip=("stored_path",)) == table_rows(
            home_a / "finance.db", table, skip=("stored_path",)
        ), table
    for sha in shas.values():
        name = f"{sha}.pdf"
        assert (home_b / "statements" / name).read_bytes() == (
            home_a / "statements" / name
        ).read_bytes()
    assert (home_b / "specs" / "my-bank.yaml").read_text(encoding="utf-8") == "name: my-bank\n"
    for sub in ("statements", "extract_cache", "specs", "logs"):
        assert (home_b / sub).is_dir()


def test_stored_paths_point_into_the_new_home(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    home_a, shas = source
    # A backup made on Windows holds backslash paths; one whose PDF was not kept holds a path too.
    con = sqlite3.connect(home_a / "finance.db")
    con.execute(
        "UPDATE statements SET stored_path = ?",
        (
            rf"C:\Users\x\AppData\Local\PersonalFinanceAnalyzer\statements\{shas['statement_sha']}.pdf",
        ),
    )
    con.execute("UPDATE tax_documents SET stored_path = '/old/machine/statements/missing.pdf'")
    con.commit()
    con.close()
    backup = tmp_path / "all.pfabackup"
    archive.create_backup(backup, passphrase=None)

    home_b = _target(tmp_path)
    archive.restore_backup(backup, passphrase=None, home=home_b)
    db = home_b / "finance.db"
    assert table_rows(db, "statements")[0][
        table_columns(db, "statements").index("stored_path")
    ] == str(home_b / "statements" / f"{shas['statement_sha']}.pdf")
    assert (
        table_rows(db, "tax_documents")[0][table_columns(db, "tax_documents").index("stored_path")]
        is None
    )


def test_archive_holds_only_allow_listed_members_and_no_secrets(
    source: tuple[Path, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, shas = source
    monkeypatch.setenv("PFA_LLM_API_KEY", "sk-env-secret")
    keychain = {("personal-finance-analyzer", "llm:llm.example.com"): SECRET_API_KEY}
    reads: list[str] = []

    def fake_get_password(service: str, account: str) -> str | None:
        reads.append(account)
        return keychain.get((service, account))

    monkeypatch.setattr(config_module.keyring, "get_password", fake_get_password)
    backup = tmp_path / "plain.pfabackup"
    archive.create_backup(backup, passphrase=None)
    assert reads == []  # a backup never even asks the keychain

    with zipfile.ZipFile(backup) as zf:
        names = set(zf.namelist())
        everything = b"".join(zf.read(n) for n in names)
        config = zf.read("config.toml").decode()
    assert names == {
        "manifest.json",
        "db/finance.db",
        "config.toml",
        "specs/my-bank.yaml",
        f"statements/{shas['statement_sha']}.pdf",
        f"statements/{shas['tax_sha']}.pdf",
    }
    # Never archived: the cache, logs, tax staging, the transient upload, the keychain/env key,
    # and anything secret-shaped in config.toml.
    for needle in (
        b"page text",
        b"a log line",
        b"%PDF-partial",
        b"sk-env-secret",
        SECRET_API_KEY.encode(),
    ):
        assert needle not in everything
    assert SECRET_HEADER not in config
    assert SECRET_URL_PASSWORD not in config
    assert "extra_headers" not in config
    assert "https://llm.example.com" in config
    assert tomllib.loads(config)["privacy"]["pii_terms"] == ["Quillfeather"]  # settings survive


def test_snapshot_drops_jobs_but_keeps_everything_else(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    home_a, _ = source
    backup = tmp_path / "plain.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    archive.restore_backup(backup, passphrase=None, home=home_b)
    assert table_rows(home_a / "finance.db", "jobs")  # the live DB still has its queued job
    assert table_rows(home_b / "finance.db", "jobs") == []


def test_snapshot_is_consistent_while_a_writer_is_open(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    home_a, _ = source
    writer = sqlite3.connect(home_a / "finance.db")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO users (name, is_default, pii_aliases, created_at) VALUES ('Z', 0, '[]', 'x')"
    )
    try:
        backup = tmp_path / "plain.pfabackup"
        manifest = archive.create_backup(backup, passphrase=None)
    finally:
        writer.rollback()
        writer.close()
    assert manifest.counts["users"] == 1  # the uncommitted row is not in the snapshot


def test_inspect_reports_manifest_without_extracting(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "all.pfabackup"
    archive.create_backup(backup, passphrase=PASSPHRASE)
    with pytest.raises(PassphraseError, match="passphrase is required"):
        archive.inspect_backup(backup)
    info = archive.inspect_backup(backup, passphrase=PASSPHRASE)
    assert info.encrypted
    assert info.manifest.schema_revision == archive.head_revision()
    assert info.manifest.format_version == archive.FORMAT_VERSION
    assert info.manifest.counts["transactions"] == 1
    assert {f.path for f in info.manifest.files} >= {"db/finance.db", "config.toml"}
    assert not _target(tmp_path).exists()


def test_wrong_passphrase_leaves_target_untouched(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "all.pfabackup"
    archive.create_backup(backup, passphrase=PASSPHRASE)
    home_b = _target(tmp_path)
    seed_home(home_b)
    before = tree(home_b)
    siblings = sorted(p.name for p in home_b.parent.iterdir())

    with pytest.raises(PassphraseError):
        archive.restore_backup(backup, passphrase="not the passphrase", replace=True, home=home_b)
    with pytest.raises(PassphraseError):
        archive.restore_backup(backup, passphrase=None, replace=True, home=home_b)

    assert tree(home_b) == before
    assert sorted(p.name for p in home_b.parent.iterdir()) == siblings  # no staging debris


def test_tampered_member_is_rejected_by_hash(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    _, shas = source
    good = tmp_path / "good.pfabackup"
    archive.create_backup(good, passphrase=None)
    tampered = tmp_path / "tampered.pfabackup"
    _rewrite_zip(
        good, tampered, {f"statements/{shas['statement_sha']}.pdf": b"%PDF-1.4 swapped content"}
    )
    home_b = _target(tmp_path)
    seed_home(home_b)
    before = tree(home_b)

    with pytest.raises(BackupFormatError, match="hash mismatch"):
        archive.restore_backup(tampered, passphrase=None, replace=True, home=home_b)
    assert tree(home_b) == before
    assert [p.name for p in home_b.parent.iterdir()] == [home_b.name]


def test_tampered_database_is_rejected(source: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    good = tmp_path / "good.pfabackup"
    archive.create_backup(good, passphrase=None)
    tampered = tmp_path / "tampered.pfabackup"
    _rewrite_zip(good, tampered, {"db/finance.db": b"SQLite format 3\x00 garbage"})
    with pytest.raises(BackupFormatError, match="hash mismatch"):
        archive.restore_backup(tampered, passphrase=None, home=_target(tmp_path))


def test_modified_encrypted_archive_is_rejected(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "all.pfabackup"
    archive.create_backup(backup, passphrase=PASSPHRASE)
    blob = bytearray(backup.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    backup.write_bytes(bytes(blob))
    with pytest.raises(PassphraseError, match="damaged or was modified"):
        archive.restore_backup(backup, passphrase=PASSPHRASE, home=_target(tmp_path))
    assert not _target(tmp_path).exists()


def test_truncated_archive_is_rejected(source: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    for passphrase in (None, PASSPHRASE):
        backup = tmp_path / f"{bool(passphrase)}.pfabackup"
        archive.create_backup(backup, passphrase=passphrase)
        backup.write_bytes(backup.read_bytes()[:-200])
        with pytest.raises(BackupError):
            archive.restore_backup(backup, passphrase=passphrase, home=_target(tmp_path))
    assert not _target(tmp_path).exists()


def test_not_a_backup_file(tmp_path: Path) -> None:
    junk = tmp_path / "junk.pfabackup"
    junk.write_bytes(b"hello")
    with pytest.raises(BackupFormatError):
        archive.inspect_backup(junk)
    plain_zip = tmp_path / "other.zip"
    with zipfile.ZipFile(plain_zip, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with pytest.raises(BackupFormatError, match="no manifest"):
        archive.inspect_backup(plain_zip)


def test_hostile_member_names_are_rejected(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    good = tmp_path / "good.pfabackup"
    archive.create_backup(good, passphrase=None)
    manifest = _manifest_of(good)
    for evil in ("../evil.txt", "statements/../../evil.txt", "/abs/evil.txt", "statements\\x.pdf"):
        doc = dict(manifest)
        files = list(manifest["files"])  # type: ignore[call-overload]
        files.append({"path": evil, "size": 1, "sha256": "0" * 64})
        doc["files"] = files
        hostile = tmp_path / "hostile.pfabackup"
        _rewrite_zip(good, hostile, {archive.MANIFEST_NAME: json.dumps(doc).encode(), evil: b"x"})
        with pytest.raises(BackupFormatError, match="unexpected file"):
            archive.restore_backup(hostile, passphrase=None, home=_target(tmp_path))
    assert not (tmp_path / "evil.txt").exists()
    assert not _target(tmp_path).exists()


def test_member_missing_from_manifest_is_rejected(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    good = tmp_path / "good.pfabackup"
    archive.create_backup(good, passphrase=None)
    smuggled = tmp_path / "smuggled.pfabackup"
    _rewrite_zip(good, smuggled, {"specs/extra.yaml": b"x: 1"})
    with pytest.raises(BackupFormatError, match="do not match its manifest"):
        archive.restore_backup(smuggled, passphrase=None, home=_target(tmp_path))


def test_member_larger_than_declared_is_aborted(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    good = tmp_path / "good.pfabackup"
    archive.create_backup(good, passphrase=None)
    manifest = _manifest_of(good)
    for entry in manifest["files"]:  # type: ignore[attr-defined]
        if entry["path"] == "specs/my-bank.yaml":
            entry["size"] = 2
    bomb = tmp_path / "bomb.pfabackup"
    _rewrite_zip(
        good,
        bomb,
        {archive.MANIFEST_NAME: json.dumps(manifest).encode(), "specs/my-bank.yaml": b"x" * 5000},
    )
    with pytest.raises(BackupFormatError, match="larger than the backup's manifest"):
        archive.restore_backup(bomb, passphrase=None, home=_target(tmp_path))


def test_newer_format_version_is_refused(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    good = tmp_path / "good.pfabackup"
    archive.create_backup(good, passphrase=None)
    manifest = _manifest_of(good)
    manifest["format_version"] = archive.FORMAT_VERSION + 1
    future = tmp_path / "future.pfabackup"
    _rewrite_zip(good, future, {archive.MANIFEST_NAME: json.dumps(manifest).encode()})
    with pytest.raises(BackupFormatError, match="newer than this version"):
        archive.inspect_backup(future)


def test_non_empty_target_refused_without_replace(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "all.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    seed_home(home_b)
    before = tree(home_b)

    with pytest.raises(RestoreTargetError, match="--replace"):
        archive.restore_backup(backup, passphrase=None, home=home_b)
    assert tree(home_b) == before
    assert [p.name for p in home_b.parent.iterdir()] == [home_b.name]


def test_replace_moves_old_dir_aside_and_keeps_it(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "all.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    seed_home(home_b)
    (home_b / "specs" / "only-on-b.yaml").write_text("keep me", encoding="utf-8")
    before = tree(home_b)

    result = archive.restore_backup(backup, passphrase=None, replace=True, home=home_b)

    assert result.previous_dir is not None
    assert result.previous_dir.parent == home_b.parent
    assert result.previous_dir.name.startswith(f"{home_b.name}.")
    assert result.previous_dir.name.endswith(".before-restore")
    assert tree(result.previous_dir) == before
    assert not (home_b / "specs" / "only-on-b.yaml").exists()
    assert (home_b / "finance.db").is_file()


def test_two_replaces_in_one_second_do_not_collide(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "all.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    first = archive.restore_backup(backup, passphrase=None, home=home_b)
    second = archive.restore_backup(backup, passphrase=None, replace=True, home=home_b)
    third = archive.restore_backup(backup, passphrase=None, replace=True, home=home_b)
    assert first.previous_dir is None
    assert second.previous_dir != third.previous_dir
    assert second.previous_dir is not None and second.previous_dir.is_dir()
    assert third.previous_dir is not None and third.previous_dir.is_dir()


def test_fresh_empty_dirs_do_not_count_as_data(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    from personal_finance_analyzer.core.paths import create_home_layout

    backup = tmp_path / "all.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    create_home_layout(home_b)  # what `ensure_home()` leaves on a machine that never ran anything
    result = archive.restore_backup(backup, passphrase=None, home=home_b)
    assert result.previous_dir is None
    assert (home_b / "finance.db").is_file()


def test_database_open_elsewhere_blocks_replace(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "all.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    seed_home(home_b)
    server = sqlite3.connect(home_b / "finance.db")  # stands in for a running `serve`
    server.execute("PRAGMA journal_mode=WAL")
    server.execute("SELECT COUNT(*) FROM users").fetchone()
    try:
        with pytest.raises(RestoreTargetError, match="open in another process"):
            archive.restore_backup(backup, passphrase=None, replace=True, home=home_b)
        assert (home_b / "finance.db").is_file()
        assert [p.name for p in home_b.parent.iterdir()] == [home_b.name]
    finally:
        server.close()
    archive.restore_backup(backup, passphrase=None, replace=True, home=home_b)


def test_before_swap_failure_aborts_cleanly(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "all.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    seed_home(home_b)
    before = tree(home_b)

    def refuse() -> None:
        raise BackupError("busy")

    with pytest.raises(BackupError, match="busy"):
        archive.restore_backup(
            backup, passphrase=None, replace=True, home=home_b, before_swap=refuse
        )
    assert tree(home_b) == before
    assert [p.name for p in home_b.parent.iterdir()] == [home_b.name]


def _make_old_schema_home(home: Path) -> None:
    """A data directory whose database was last migrated at 0005, before the provenance columns."""
    from personal_finance_analyzer.core.paths import create_home_layout

    create_home_layout(home)
    url = f"sqlite:///{home / 'finance.db'}"
    command.upgrade(build_alembic_config(url), "0005_balance_entries")
    con = sqlite3.connect(home / "finance.db")
    con.execute(
        "INSERT INTO users (name, is_default, pii_aliases, created_at) "
        "VALUES ('Old User', 1, '[]', 'then')"
    )
    con.commit()
    con.close()


def test_older_schema_is_migrated_on_restore(tmp_path: Path) -> None:
    old = tmp_path / "old_machine"
    _make_old_schema_home(old)
    backup = tmp_path / "old.pfabackup"
    manifest = archive.create_backup(backup, passphrase=None, home=old)
    assert manifest.schema_revision == "0005_balance_entries"

    home_b = _target(tmp_path)
    result = archive.restore_backup(backup, passphrase=None, home=home_b)

    assert result.migrated
    db = home_b / "finance.db"
    assert "extraction_method" in table_columns(db, "tax_forms")  # added by 0006
    revisions = table_rows(db, "alembic_version")
    assert revisions == [(archive.head_revision(),)]
    assert ("Old User",) in [(r[1],) for r in table_rows(db, "users")]
    assert table_rows(db, "categories")  # taxonomy was re-synced into the migrated database


def test_newer_schema_is_refused_with_clear_message(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    home_a, _ = source
    con = sqlite3.connect(home_a / "finance.db")
    con.execute("UPDATE alembic_version SET version_num = '0099_from_the_future'")
    con.commit()
    con.close()
    backup = tmp_path / "future.pfabackup"
    archive.create_backup(backup, passphrase=None)

    home_b = _target(tmp_path)
    seed_home(home_b)
    before = tree(home_b)
    with pytest.raises(
        SchemaTooNewError, match="newer than this version of Personal Finance Analyzer"
    ):
        archive.restore_backup(backup, passphrase=None, replace=True, home=home_b)
    assert tree(home_b) == before
    assert [p.name for p in home_b.parent.iterdir()] == [home_b.name]


def test_create_requires_a_database_and_a_real_passphrase(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BackupError, match="nothing to back up"):
        archive.create_backup(tmp_path / "x.pfabackup", passphrase=None, home=empty)
    home = tmp_path / "h"
    seed_home(home)
    with pytest.raises(BackupError, match="at least"):
        archive.create_backup(tmp_path / "x.pfabackup", passphrase="short", home=home)
    assert not (tmp_path / "x.pfabackup").exists()


def test_create_failure_leaves_no_partial_file(tmp_path: Path) -> None:
    home = tmp_path / "h"
    seed_home(home)
    (home / "config.toml").write_text("this is [not valid toml", encoding="utf-8")
    dest = tmp_path / "out.pfabackup"
    with pytest.raises(BackupError, match="not valid TOML"):
        archive.create_backup(dest, passphrase=None, home=home)
    assert list(tmp_path.glob("out.*")) == []


def test_config_without_llm_section_round_trips(tmp_path: Path) -> None:
    home = tmp_path / "h"
    seed_home(home)
    (home / "config.toml").write_text("[server]\nport = 9000\n", encoding="utf-8")
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None, home=home)
    target = _target(tmp_path)
    archive.restore_backup(backup, passphrase=None, home=target)
    assert "port = 9000" in (target / "config.toml").read_text(encoding="utf-8")


def test_restored_database_and_description_survive(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=PASSPHRASE)
    home_b = _target(tmp_path)
    archive.restore_backup(backup, passphrase=PASSPHRASE, home=home_b)
    con = sqlite3.connect(home_b / "finance.db")
    try:
        assert con.execute("SELECT name FROM users").fetchone() == (USER_NAME,)
        assert con.execute("SELECT description_raw FROM transactions").fetchone() == (DESCRIPTION,)
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        con.close()
    # The restored config is the sanitized one, byte-for-byte parseable settings.
    assert (home_b / "config.toml").read_text(encoding="utf-8") != CONFIG_TOML


def test_default_filename_has_extension() -> None:
    assert archive.default_filename().endswith(archive.FILE_EXTENSION)


def test_encrypt_stream_is_used_for_passphrases(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    plain = tmp_path / "p.pfabackup"
    sealed = tmp_path / "s.pfabackup"
    archive.create_backup(plain, passphrase=None)
    archive.create_backup(sealed, passphrase=PASSPHRASE)
    assert zipfile.is_zipfile(plain)
    assert not zipfile.is_zipfile(sealed)
    out = io.BytesIO()
    with sealed.open("rb") as fh:
        crypto.decrypt_stream(fh, out, PASSPHRASE)
    assert zipfile.is_zipfile(io.BytesIO(out.getvalue()))
