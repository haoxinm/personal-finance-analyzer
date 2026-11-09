"""Backup and restore of the documents folder: PDFs travel under their readable names, a restore
writes into the folder in place (never replacing it, never deleting what is already there), and
archives made before the folder existed still restore."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from personal_finance_analyzer.backup import archive
from personal_finance_analyzer.backup.errors import BackupError, BackupFormatError
from personal_finance_analyzer.core import documents as docs

from .support import STATEMENT_REL, TAX_REL, pdf_bytes, seed_home, table_rows, tree


def _target(tmp_path: Path) -> Path:
    return tmp_path / "machine_b" / "PersonalFinanceAnalyzer"


def _members(backup: Path) -> set[str]:
    with zipfile.ZipFile(backup) as zf:
        return set(zf.namelist())


def _stored_paths(home: Path) -> dict[str, str | None]:
    con = sqlite3.connect(home / "finance.db")
    try:
        return {
            "statement": con.execute("SELECT stored_path FROM statements").fetchone()[0],
            "tax": con.execute("SELECT stored_path FROM tax_documents").fetchone()[0],
        }
    finally:
        con.close()


# --- backing up ---------------------------------------------------------------------------------


def test_backup_holds_the_documents_folder_under_a_documents_prefix(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    home_a, _ = source
    (home_a / "documents" / "notes.txt").write_text("not a pdf", encoding="utf-8")
    (home_a / "documents" / ".hidden").mkdir()
    (home_a / "documents" / ".hidden" / "x.pdf").write_bytes(pdf_bytes("hidden"))
    backup = tmp_path / "b.pfabackup"
    manifest = archive.create_backup(backup, passphrase=None)
    names = _members(backup)
    assert f"documents/{STATEMENT_REL}" in names and f"documents/{TAX_REL}" in names
    assert not any(n.endswith("notes.txt") or ".hidden" in n for n in names)
    assert not any(n.startswith("statements/") for n in names)  # nothing in the old layout
    assert manifest.skipped == ()
    sizes = {f.path: f.size for f in manifest.files}
    assert sizes[f"documents/{STATEMENT_REL}"] == len(pdf_bytes("statement"))


def test_a_file_with_an_unportable_name_is_skipped_and_reported(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    home_a, _ = source
    odd = home_a / "documents" / "Alex" / "what? it's: a *name*.pdf"
    odd.parent.mkdir(parents=True, exist_ok=True)
    try:
        odd.write_bytes(pdf_bytes("odd"))
    except OSError:  # pragma: no cover - a filesystem that cannot hold the name at all
        pytest.skip("the filesystem rejects the name")
    backup = tmp_path / "b.pfabackup"
    manifest = archive.create_backup(backup, passphrase=None)
    assert manifest.skipped == ("Alex/what? it's: a *name*.pdf",)
    assert not any("what?" in n for n in _members(backup))


def test_documents_folder_can_live_elsewhere(tmp_path: Path, home: Path) -> None:
    nas = tmp_path / "nas" / "docs"
    shas = seed_home(home, documents=nas)
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None, home=home, documents=nas)
    assert f"documents/{shas['statement_rel']}" in _members(backup)
    # ...and with no `documents=` the default folder (inside the data dir) is empty: no PDFs.
    empty = tmp_path / "empty.pfabackup"
    archive.create_backup(empty, passphrase=None, home=home)
    assert not any(n.startswith("documents/") for n in _members(empty))


def test_awaiting_uploads_travel_in_incoming(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    home_a, _ = source
    sha = hashlib.sha256(pdf_bytes("waiting")).hexdigest()
    (home_a / "incoming" / f"{sha}.pdf").write_bytes(pdf_bytes("waiting"))
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None)
    assert f"incoming/{sha}.pdf" in _members(backup)
    home_b = _target(tmp_path)
    archive.restore_backup(backup, passphrase=None, home=home_b)
    assert (home_b / "incoming" / f"{sha}.pdf").read_bytes() == pdf_bytes("waiting")


# --- restoring in place -------------------------------------------------------------------------


def test_restore_never_touches_what_is_already_in_the_documents_folder(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    _, shas = source
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None)

    home_b = _target(tmp_path)
    nas = tmp_path / "nas"  # a share the user already uses for other things
    mine = nas / "Taxes" / "2019 return.pdf"
    mine.parent.mkdir(parents=True)
    mine.write_bytes(b"%PDF- my own file")
    (nas / "holiday.jpg").write_bytes(b"jpeg")
    before = tree(nas)

    result = archive.restore_backup(backup, passphrase=None, home=home_b, documents=nas)

    after = tree(nas)
    for name, data in before.items():
        assert after[name] == data  # every pre-existing file and folder is exactly as it was
    assert (nas / shas["statement_rel"]).read_bytes() == pdf_bytes("statement")
    assert result.documents_restored == 2
    assert result.documents_unrelated == 2  # the PDF and the JPEG that are not part of the backup
    assert result.documents_pending is None
    assert not [p for p in nas.iterdir() if p.name.startswith(".pfa-restore")]  # staging is gone
    assert not (home_b / "documents").exists()


def test_a_file_already_there_with_the_same_content_is_reused(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    _, shas = source
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    nas = tmp_path / "nas"
    existing = nas / shas["statement_rel"]
    existing.parent.mkdir(parents=True)
    existing.write_bytes(pdf_bytes("statement"))
    mtime = existing.stat().st_mtime_ns

    result = archive.restore_backup(backup, passphrase=None, home=home_b, documents=nas)

    assert result.documents_already_present == 1 and result.documents_restored == 1
    assert existing.stat().st_mtime_ns == mtime  # not rewritten
    assert _stored_paths(home_b)["statement"] == shas["statement_rel"]


def test_a_name_taken_by_a_different_file_gets_a_number_and_the_database_follows(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    _, shas = source
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    nas = tmp_path / "nas"
    taken = nas / shas["statement_rel"]
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"%PDF- somebody else's statement with the same name")

    archive.restore_backup(backup, passphrase=None, home=home_b, documents=nas)

    assert taken.read_bytes().startswith(b"%PDF- somebody else's")  # untouched
    numbered = shas["statement_rel"].replace(".pdf", " (2).pdf")
    assert (nas / numbered).read_bytes() == pdf_bytes("statement")
    assert _stored_paths(home_b)["statement"] == numbered
    # The restored database resolves to the right PDF.
    path = docs.kept_pdf(numbered, shas["statement_sha"], nas)
    assert path == nas / numbered


def test_the_default_folder_inside_the_data_directory_stays_in_place(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    seed_home(home_b)
    mine = home_b / "documents" / "Alex" / "my own scan.pdf"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_bytes(b"%PDF- mine")

    result = archive.restore_backup(backup, passphrase=None, replace=True, home=home_b)

    assert mine.read_bytes() == b"%PDF- mine"  # not swept off to *.before-restore
    assert result.previous_dir is not None
    assert not (result.previous_dir / "documents").exists()
    assert (home_b / "documents" / STATEMENT_REL).is_file()
    assert result.documents_unrelated == 1


def test_a_failed_restore_leaves_the_folder_exactly_as_it_was(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    nas = tmp_path / "nas"
    (nas / "Alex").mkdir(parents=True)
    (nas / "Alex" / "mine.pdf").write_bytes(b"%PDF- mine")
    before = tree(nas)

    def refuse() -> None:
        raise BackupError("busy")

    with pytest.raises(BackupError, match="busy"):
        archive.restore_backup(
            backup, passphrase=None, home=home_b, documents=nas, before_swap=refuse
        )
    assert tree(nas) == before  # nothing moved in, and the hidden staging folder is gone
    assert not home_b.exists()


def _refuse_swap(*_args: object, **_kwargs: object) -> Path:
    raise OSError("disk went away")


def test_a_failed_swap_puts_the_documents_folder_back(
    source: tuple[Path, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    seed_home(home_b)
    mine = home_b / "documents" / "Alex" / "my own scan.pdf"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_bytes(b"%PDF- mine")
    monkeypatch.setattr(archive, "_move_aside", _refuse_swap)

    with pytest.raises(BackupError, match="could not move"):
        archive.restore_backup(backup, passphrase=None, replace=True, home=home_b)

    assert mine.read_bytes() == b"%PDF- mine"
    assert not [p for p in home_b.parent.iterdir() if ".restore-" in p.name]  # stage cleaned up


def test_a_failed_swap_and_failed_rollback_never_deletes_the_documents_folder(
    source: tuple[Path, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    seed_home(home_b)
    mine = home_b / "documents" / "Alex" / "my own scan.pdf"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_bytes(b"%PDF- mine")
    monkeypatch.setattr(archive, "_move_aside", _refuse_swap)
    monkeypatch.setattr(archive, "_undo_carry", _refuse_swap)

    with pytest.raises(BackupError, match="could NOT be put back") as caught:
        archive.restore_backup(backup, passphrase=None, replace=True, home=home_b)

    stages = [p for p in home_b.parent.iterdir() if ".restore-" in p.name]
    assert len(stages) == 1  # left in place, not deleted
    stranded = next(stages[0].rglob("my own scan.pdf"))
    assert stranded.read_bytes() == b"%PDF- mine"
    assert str(stages[0]) in str(caught.value)  # and the message says where it is
    assert any(str(stages[0]) in r.getMessage() for r in caplog.records)


def test_documents_are_moved_in_only_after_the_swap(
    source: tuple[Path, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    nas = tmp_path / "nas"
    seen: dict[str, bool] = {}

    def before_swap() -> None:
        seen["pdfs_visible"] = any(True for _ in docs.iter_files(nas))
        seen["staged"] = any(p.name.startswith(".pfa-restore-") for p in nas.iterdir())

    archive.restore_backup(
        backup, passphrase=None, home=home_b, documents=nas, before_swap=before_swap
    )
    assert seen == {"pdfs_visible": False, "staged": True}  # staged, hidden, not yet in place


def test_a_failure_moving_files_in_is_reported_and_nothing_is_lost(
    source: tuple[Path, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    nas = tmp_path / "nas"

    def refuse(old: Path, new: Path) -> None:
        raise PermissionError("share went read-only")

    monkeypatch.setattr(docs, "publish_no_clobber_rename", refuse)
    result = archive.restore_backup(backup, passphrase=None, home=home_b, documents=nas)

    assert (home_b / "finance.db").is_file()  # the data directory is restored
    assert result.documents_restored == 0 and result.documents_pending is not None
    pending = result.documents_pending
    assert pending.parent == nas and pending.is_dir()
    assert sorted(
        p.name for p in docs.iter_files(pending, pdf_only=True) and pending.rglob("*.pdf")
    ) == [
        "2026-01 Fixture Bank credit 9876.pdf",
        "W-2 - Acme Corp.pdf",
    ]  # the files are still there, to be moved in by hand


def test_an_unwritable_documents_folder_stops_before_anything_changes(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None)
    home_b = _target(tmp_path)
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(BackupError, match="cannot write to the documents folder"):
        archive.restore_backup(backup, passphrase=None, home=home_b, documents=blocker / "docs")
    assert not home_b.exists()


def test_a_backup_without_documents_leaves_the_folder_alone(tmp_path: Path, home: Path) -> None:
    import os

    from personal_finance_analyzer.core.paths import create_home_layout
    from personal_finance_analyzer.db.migrate import upgrade_head

    src = tmp_path / "bare"
    create_home_layout(src)
    upgrade_head(f"sqlite:///{src / 'finance.db'}")
    backup = tmp_path / "bare.pfabackup"
    archive.create_backup(backup, passphrase=None, home=src)
    nas = tmp_path / "nas"
    (nas).mkdir()
    (nas / "mine.pdf").write_bytes(b"%PDF- mine")
    result = archive.restore_backup(backup, passphrase=None, home=_target(tmp_path), documents=nas)
    assert result.documents_dir is None and result.documents_restored == 0
    assert os.listdir(nas) == ["mine.pdf"]


# --- hostile member names -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "evil",
    [
        "documents/../evil.pdf",
        "documents/a/../../evil.pdf",
        "documents//x.pdf",
        "documents/.hidden/x.pdf",
        "documents/C:/x.pdf",
        "documents/a\\b.pdf",
        "documents/x.txt",
        "documents/x.pdf ",
        "documents/CON?.pdf",
        "documents/" + "a/" * 20 + "x.pdf",
        "documents/" + "n" * 300 + ".pdf",
    ],
)
def test_hostile_document_members_are_rejected(
    source: tuple[Path, dict[str, str]], tmp_path: Path, evil: str
) -> None:
    good = tmp_path / "good.pfabackup"
    archive.create_backup(good, passphrase=None)
    with zipfile.ZipFile(good) as zf:
        manifest = json.loads(zf.read(archive.MANIFEST_NAME))
    manifest["files"].append({"path": evil, "size": 1, "sha256": "0" * 64})
    hostile = tmp_path / "hostile.pfabackup"
    with zipfile.ZipFile(good) as zin, zipfile.ZipFile(hostile, "w") as zout:
        for name in zin.namelist():
            if name != archive.MANIFEST_NAME:
                zout.writestr(name, zin.read(name))
        zout.writestr(archive.MANIFEST_NAME, json.dumps(manifest))
        zout.writestr(evil, b"x")
    nas = tmp_path / "nas"
    with pytest.raises(BackupFormatError, match="unexpected file"):
        archive.restore_backup(hostile, passphrase=None, home=_target(tmp_path), documents=nas)
    assert not (tmp_path / "evil.pdf").exists()
    assert not nas.exists() or not any(nas.rglob("*"))


# --- archives from before the documents folder --------------------------------------------------


def test_an_old_format_archive_is_converted_to_the_readable_layout(
    tmp_path: Path, home: Path
) -> None:
    """``statements/<sha>.pdf`` members, absolute paths from the machine that made it."""
    old_machine = tmp_path / "old_machine"
    shas = seed_home(old_machine, legacy=True)
    backup = tmp_path / "old.pfabackup"
    manifest = archive.create_backup(backup, passphrase=None, home=old_machine)
    names = {f.path for f in manifest.files}
    assert f"statements/{shas['statement_sha']}.pdf" in names
    assert not any(n.startswith("documents/") for n in names)
    assert "statements/tmpabcd1234.pdf" not in names  # a partial upload is never archived

    home_b = _target(tmp_path)
    nas = tmp_path / "nas"
    result = archive.restore_backup(backup, passphrase=None, home=home_b, documents=nas)

    assert _stored_paths(home_b) == {"statement": STATEMENT_REL, "tax": TAX_REL}
    assert (nas / STATEMENT_REL).read_bytes() == pdf_bytes("statement")
    assert (nas / TAX_REL).read_bytes() == pdf_bytes("taxform")
    assert result.documents_restored == 2
    assert not (home_b / "statements").exists()  # nothing is left in the old layout
    assert table_rows(home_b / "finance.db", "transactions") == table_rows(
        old_machine / "finance.db", "transactions"
    )


def test_an_old_format_archive_with_windows_paths_still_restores(
    tmp_path: Path, home: Path
) -> None:
    old_machine = tmp_path / "old_machine"
    shas = seed_home(old_machine, legacy=True)
    con = sqlite3.connect(old_machine / "finance.db")
    con.execute(
        "UPDATE statements SET stored_path = ?",
        (
            rf"C:\Users\x\AppData\Local\PersonalFinanceAnalyzer\statements\{shas['statement_sha']}.pdf",
        ),
    )
    con.commit()
    con.close()
    backup = tmp_path / "old.pfabackup"
    archive.create_backup(backup, passphrase=None, home=old_machine)
    home_b = _target(tmp_path)
    archive.restore_backup(backup, passphrase=None, home=home_b, documents=tmp_path / "nas")
    assert _stored_paths(home_b)["statement"] == STATEMENT_REL


def test_an_old_format_archive_restores_next_to_files_already_in_the_folder(
    tmp_path: Path, home: Path
) -> None:
    old_machine = tmp_path / "old_machine"
    seed_home(old_machine, legacy=True)
    backup = tmp_path / "old.pfabackup"
    archive.create_backup(backup, passphrase=None, home=old_machine)
    nas = tmp_path / "nas"
    clash = nas / STATEMENT_REL
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"%PDF- already here, different")
    result = archive.restore_backup(backup, passphrase=None, home=_target(tmp_path), documents=nas)
    assert clash.read_bytes() == b"%PDF- already here, different"
    assert result.documents_restored == 2
    assert _stored_paths(_target(tmp_path))["statement"] == STATEMENT_REL.replace(
        ".pdf", " (2).pdf"
    )


def test_a_backup_made_before_the_startup_upgrade_ran_restores_too(
    tmp_path: Path, home: Path
) -> None:
    """A *new* archive of a data directory whose old files have not been carried over yet holds
    them as ``statements/`` members and relative-free (absolute) database paths."""
    src = tmp_path / "pending"
    shas = seed_home(src, legacy=True)
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=None, home=src)
    home_b = _target(tmp_path)
    archive.restore_backup(backup, passphrase=None, home=home_b, documents=tmp_path / "nas")
    assert (tmp_path / "nas" / STATEMENT_REL).is_file()
    assert shas["tax_sha"]
