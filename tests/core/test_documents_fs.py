"""The documents folder on disk: placing files, collisions, finding by content after a manual move,
permissions, README, and removal."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from personal_finance_analyzer.core import documents as docs
from personal_finance_analyzer.core.documents import DocumentsError
from personal_finance_analyzer.core.paths import documents_dir

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


def _pdf(tmp_path: Path, tag: str) -> tuple[Path, str]:
    data = b"%PDF-1.4\n" + tag.encode() * 100
    path = tmp_path / f"src-{tag}.pdf"
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


# --- where the folder is --------------------------------------------------------------------------


def test_default_documents_dir_is_inside_the_data_directory(home: Path) -> None:
    assert documents_dir() == home / "documents"


def test_env_overrides_the_documents_dir(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PFA_DOCUMENTS_DIR", str(tmp_path / "nas" / "docs"))
    assert documents_dir() == tmp_path / "nas" / "docs"


def test_config_toml_sets_the_documents_dir_and_env_wins(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / "config.toml").write_text(
        f'[storage]\ndocuments_dir = "{(tmp_path / "from-config").as_posix()}"\n', encoding="utf-8"
    )
    assert documents_dir() == tmp_path / "from-config"
    monkeypatch.setenv("PFA_DOCUMENTS_DIR", str(tmp_path / "from-env"))
    assert documents_dir() == tmp_path / "from-env"


def test_a_relative_config_path_is_relative_to_the_data_directory(home: Path) -> None:
    (home / "config.toml").write_text('[storage]\ndocuments_dir = "pdfs"\n', encoding="utf-8")
    assert documents_dir() == home / "pdfs"


def test_a_broken_config_falls_back_to_the_default(home: Path) -> None:
    (home / "config.toml").write_text("[storage\nnot toml", encoding="utf-8")
    assert documents_dir() == home / "documents"


# --- placing --------------------------------------------------------------------------------------


def test_place_file_copies_without_touching_the_source(home: Path, tmp_path: Path) -> None:
    src, sha = _pdf(tmp_path, "a")
    rel = docs.place_file(src, "Alex/Statements/Chase", "2024-03 Chase credit 1234", sha)
    assert rel == "Alex/Statements/Chase/2024-03 Chase credit 1234.pdf"
    assert (documents_dir() / rel).read_bytes() == src.read_bytes()
    assert src.exists()
    # No temp files are left behind in the destination folder.
    assert [p.name for p in (documents_dir() / "Alex/Statements/Chase").iterdir()] == [
        "2024-03 Chase credit 1234.pdf"
    ]


def test_placing_the_same_content_twice_reuses_the_file(home: Path, tmp_path: Path) -> None:
    src, sha = _pdf(tmp_path, "a")
    first = docs.place_file(src, "Alex/Tax/2024", "W-2 - Acme", sha)
    again = docs.place_file(src, "Alex/Tax/2024", "W-2 - Acme", sha)
    assert first == again == "Alex/Tax/2024/W-2 - Acme.pdf"
    assert len(list((documents_dir() / "Alex/Tax/2024").iterdir())) == 1


def test_different_content_with_the_same_name_gets_a_number(home: Path, tmp_path: Path) -> None:
    (a, sha_a), (b, sha_b), (c, sha_c) = (_pdf(tmp_path, t) for t in ("a", "b", "c"))
    assert docs.place_file(a, "F", "same", sha_a) == "F/same.pdf"
    assert docs.place_file(b, "F", "same", sha_b) == "F/same (2).pdf"
    assert docs.place_file(c, "F", "same", sha_c) == "F/same (3).pdf"
    assert docs.place_file(b, "F", "same", sha_b) == "F/same (2).pdf"  # idempotent per content


def test_collisions_ignore_letter_case(home: Path, tmp_path: Path) -> None:
    (a, sha_a), (b, sha_b) = _pdf(tmp_path, "a"), _pdf(tmp_path, "b")
    docs.place_file(a, "F", "Statement", sha_a)
    assert docs.place_file(b, "F", "STATEMENT", sha_b) == "F/STATEMENT (2).pdf"


def test_a_name_claimed_by_another_record_is_skipped(home: Path, tmp_path: Path) -> None:
    src, sha = _pdf(tmp_path, "a")
    rel = docs.place_file(src, "F", "x", sha, claimed=lambda r: r == "F/x.pdf")
    assert rel == "F/x (2).pdf"


def test_place_file_refuses_a_file_whose_content_changed(home: Path, tmp_path: Path) -> None:
    src, _sha = _pdf(tmp_path, "a")
    with pytest.raises(DocumentsError):
        docs.place_file(src, "F", "x", "0" * 64)
    assert not list(documents_dir().rglob("*.pdf"))
    assert not list(documents_dir().rglob(".*.part"))  # and no temp file either


def test_an_existing_file_is_never_overwritten(home: Path, tmp_path: Path) -> None:
    src, sha = _pdf(tmp_path, "a")
    target = documents_dir() / "F"
    target.mkdir(parents=True)
    (target / "x.pdf").write_bytes(b"the user's own file")
    assert docs.place_file(src, "F", "x", sha) == "F/x (2).pdf"
    assert (target / "x.pdf").read_bytes() == b"the user's own file"


def test_readme_is_written_once_and_never_clobbered(home: Path) -> None:
    root = docs.ensure_documents_dir()
    readme = root / docs.README_NAME
    assert "never overwrites your edits" in readme.read_text(encoding="utf-8")
    readme.write_text("my own notes", encoding="utf-8")
    docs.ensure_documents_dir()
    assert readme.read_text(encoding="utf-8") == "my own notes"
    readme.unlink()
    docs.ensure_documents_dir()
    assert readme.exists()  # regenerated when missing


def test_readme_is_not_listed_as_a_document(home: Path) -> None:
    docs.ensure_documents_dir()
    assert list(docs.iter_files(pdf_only=False)) == []


# --- permissions -----------------------------------------------------------------------------------


@posix_only
def test_default_permissions_are_private(home: Path, tmp_path: Path) -> None:
    src, sha = _pdf(tmp_path, "a")
    rel = docs.place_file(src, "Alex/Statements/Chase", "s", sha)
    path = documents_dir() / rel
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for directory in (documents_dir(), path.parent, path.parent.parent, path.parent.parent.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@posix_only
def test_an_existing_directory_is_never_chmodded(home: Path, tmp_path: Path) -> None:
    share = tmp_path / "share"
    share.mkdir()
    share.chmod(0o755)
    src, sha = _pdf(tmp_path, "a")
    docs.place_file(src, "Alex", "s", sha, root=share)
    assert stat.S_IMODE(share.stat().st_mode) == 0o755  # the mount point / share root is theirs
    assert stat.S_IMODE((share / "Alex").stat().st_mode) == 0o700


@posix_only
def test_in_a_container_the_umask_decides(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PFA_CONTAINER", "1")
    previous = os.umask(0o027)
    try:
        monkeypatch.setattr(docs, "_UMASK", 0o027)
        src, sha = _pdf(tmp_path, "a")
        rel = docs.place_file(src, "Alex/Tax/2024", "f", sha, root=tmp_path / "docs")
        path = tmp_path / "docs" / rel
        assert stat.S_IMODE(path.stat().st_mode) == 0o640  # 0666 & ~027
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o750  # 0777 & ~027
    finally:
        os.umask(previous)


# --- finding a file --------------------------------------------------------------------------------


def test_kept_pdf_prefers_the_recorded_path(home: Path, tmp_path: Path) -> None:
    src, sha = _pdf(tmp_path, "a")
    rel = docs.place_file(src, "F", "x", sha)
    assert docs.kept_pdf(rel, sha) == documents_dir() / rel
    assert docs.kept_pdf(None, sha) is None  # a row that kept no copy has none


def test_kept_pdf_finds_a_file_the_user_moved_or_renamed(home: Path, tmp_path: Path) -> None:
    src, sha = _pdf(tmp_path, "a")
    rel = docs.place_file(src, "Alex/Statements/Chase", "2024-03 Chase", sha)
    moved = documents_dir() / "My own folder" / "march statement final.pdf"
    moved.parent.mkdir()
    (documents_dir() / rel).rename(moved)
    assert docs.kept_pdf(rel, sha) == moved
    # ...and after they move it again (the index notices the change on disk).
    again = documents_dir() / "elsewhere.pdf"
    moved.rename(again)
    assert docs.kept_pdf(rel, sha) == again


def test_kept_pdf_is_none_once_the_file_is_deleted(home: Path, tmp_path: Path) -> None:
    src, sha = _pdf(tmp_path, "a")
    rel = docs.place_file(src, "F", "x", sha)
    (documents_dir() / rel).unlink()
    assert docs.kept_pdf(rel, sha) is None


def test_the_content_index_does_not_confuse_files(home: Path, tmp_path: Path) -> None:
    (a, sha_a), (b, sha_b) = _pdf(tmp_path, "a"), _pdf(tmp_path, "b")
    docs.place_file(a, "F", "one", sha_a)
    docs.place_file(b, "F", "two", sha_b)
    assert docs.find_by_sha(sha_b) == documents_dir() / "F" / "two.pdf"
    assert docs.find_by_sha("1" * 64) is None


def test_a_list_lookup_never_walks_or_hashes_the_folder(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, sha = _pdf(tmp_path, "a")
    rel = docs.place_file(src, "F", "x", sha)
    moved = documents_dir() / "scans" / "x.pdf"
    moved.parent.mkdir()
    (documents_dir() / rel).rename(moved)
    docs.invalidate_index()

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a list view must not walk or hash the folder")

    with monkeypatch.context() as m:
        m.setattr(docs, "iter_files", boom)
        m.setattr(docs, "file_sha256", boom)
        assert docs.kept_pdf(rel, sha, search=False) is None  # not known yet: "not found"
        assert docs.kept_pdf("nope/x.pdf", "9" * 64, search=False) is None
    assert docs.kept_pdf(rel, sha) == moved  # the on-demand lookup finds it...
    with monkeypatch.context() as m:
        m.setattr(docs, "iter_files", boom)
        assert docs.kept_pdf(rel, sha, search=False) == moved  # ...and the list now knows too
    moved.unlink()
    assert docs.kept_pdf(rel, sha, search=False) is None  # a stale cache entry is not believed


def test_the_search_does_not_hold_the_lock_while_hashing(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (a, sha_a), (b, sha_b) = _pdf(tmp_path, "a"), _pdf(tmp_path, "b")
    docs.place_file(a, "F", "one", sha_a)
    docs.place_file(b, "F", "two", sha_b)
    docs.invalidate_index()
    real = docs.file_sha256
    held: list[bool] = []

    def spy(path: Path) -> str | None:
        held.append(docs._INDEX_LOCK.locked())
        return real(path)

    monkeypatch.setattr(docs, "file_sha256", spy)
    assert docs.find_by_sha(sha_b) is not None
    assert docs.ShaIndex().find(sha_a) is not None
    assert held and not any(held)


def test_sha_index_walks_once_for_many_lookups(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (a, sha_a), (b, sha_b) = _pdf(tmp_path, "a"), _pdf(tmp_path, "b")
    docs.place_file(a, "F", "one", sha_a)
    docs.place_file(b, "F", "two", sha_b)
    walks: list[int] = []
    real = docs.iter_files

    def counting(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        walks.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(docs, "iter_files", counting)
    index = docs.ShaIndex()
    assert index.find(sha_a) == documents_dir() / "F" / "one.pdf"
    assert index.find(sha_b) == documents_dir() / "F" / "two.pdf"
    assert index.find("1" * 64) is None
    assert len(walks) == 1


def test_hidden_entries_and_symlinks_are_not_searched(home: Path, tmp_path: Path) -> None:
    src, sha = _pdf(tmp_path, "a")
    hidden = documents_dir() / ".pfa-restore-x"
    hidden.mkdir(parents=True)
    (hidden / "x.pdf").write_bytes(src.read_bytes())
    assert docs.find_by_sha(sha) is None
    if sys.platform != "win32":
        (documents_dir() / "link.pdf").symlink_to(src)
        assert docs.find_by_sha(sha) is None


def test_a_stored_path_cannot_climb_out_of_the_folder(home: Path) -> None:
    assert docs.resolve("../secrets/x.pdf") is None
    assert docs.resolve("a/../../x.pdf") is None
    assert docs.resolve("") is None
    assert docs.resolve("a\\..\\x.pdf") is None
    assert docs.resolve("Alex/x.pdf") == documents_dir() / "Alex" / "x.pdf"
    absolute = home / "old" / "statements" / "x.pdf"  # a path written by an older version
    assert docs.resolve(str(absolute)) == absolute


# --- removing --------------------------------------------------------------------------------------


def test_discard_removes_the_file_and_the_folders_it_leaves_empty(
    home: Path, tmp_path: Path
) -> None:
    (a, sha_a), (b, sha_b) = _pdf(tmp_path, "a"), _pdf(tmp_path, "b")
    one = docs.place_file(a, "Alex/Statements/Chase", "one", sha_a)
    two = docs.place_file(b, "Alex/Statements/Chase", "two", sha_b)
    assert docs.discard_path(documents_dir() / one)
    assert (documents_dir() / two).exists()  # the folder still has a file
    assert docs.discard_path(documents_dir() / two)
    assert not (documents_dir() / "Alex/Statements/Chase").exists()  # the institution folder goes
    assert (documents_dir() / "Alex/Statements").is_dir()  # but never the drop folders
    assert documents_dir().is_dir()


def test_prune_never_removes_the_root_person_statements_or_tax_folders(
    home: Path,
) -> None:
    root = documents_dir()
    leaf = root / "Alex" / "Tax" / "2024"
    other = root / "Alex" / "Statements" / "Chase"
    shared = root / "Shared"
    for folder in (leaf, other, shared):
        folder.mkdir(parents=True)
    docs.prune_empty_dirs(leaf, root)
    docs.prune_empty_dirs(other, root)
    docs.prune_empty_dirs(shared, root)
    assert not leaf.exists() and not other.exists()
    assert (root / "Alex" / "Tax").is_dir() and (root / "Alex" / "Statements").is_dir()
    assert (root / "Alex").is_dir() and shared.is_dir() and root.is_dir()
    docs.prune_empty_dirs(root / "Alex" / "Tax", root)  # asked directly: still kept
    assert (root / "Alex" / "Tax").is_dir()


def test_discard_leaves_folders_with_other_files(home: Path, tmp_path: Path) -> None:
    src, sha = _pdf(tmp_path, "a")
    rel = docs.place_file(src, "Alex/Tax/2024", "f", sha)
    (documents_dir() / "Alex" / "Tax" / "notes.txt").write_text("mine", encoding="utf-8")
    docs.discard_path(documents_dir() / rel)
    assert not (documents_dir() / "Alex/Tax/2024").exists()
    assert (documents_dir() / "Alex/Tax/notes.txt").read_text(encoding="utf-8") == "mine"
