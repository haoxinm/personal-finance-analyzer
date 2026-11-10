"""Filesystem-type detection and the SQLite journal-mode fallback for network shares."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from personal_finance_analyzer.core.fs_check import filesystem_type, is_network_filesystem
from personal_finance_analyzer.db.session import (
    JOURNAL_MODE_ENV,
    choose_journal_mode,
    make_engine_for_path,
    storage_warning,
)

# `ID parent major:minor root mount-point options [optional] - fstype source super-options`
MOUNTINFO = r"""
22 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw
41 22 0:38 / /pfa rw,relatime - ext4 /dev/sdb1 rw
42 41 0:39 / /pfa/documents rw,relatime shared:5 - nfs4 nas:/export/docs rw,vers=4.2
43 22 0:40 / /mnt/with\040space rw - cifs //nas/share rw
44 22 0:41 / /pfabackup rw - fuse.sshfs me@host:/ rw
"""

# The mount table is a Linux file, and on Windows `Path("/pfa/x").resolve()` gains a drive letter
# (`C:/pfa/x`), so a POSIX mount point can never be a prefix of it: these cases are Linux-only.
needs_posix_paths = pytest.mark.skipif(
    sys.platform == "win32", reason="mountinfo paths are POSIX; Windows resolves them to a drive"
)


@needs_posix_paths
def test_longest_mount_point_wins(tmp_path: Path) -> None:
    assert filesystem_type(Path("/etc/hosts"), mountinfo=MOUNTINFO) == "ext4"
    assert filesystem_type(Path("/pfa/data"), mountinfo=MOUNTINFO) == "ext4"
    assert filesystem_type(Path("/pfa/documents/2025"), mountinfo=MOUNTINFO) == "nfs4"


@needs_posix_paths
def test_a_mount_point_is_not_a_prefix_of_a_sibling_name() -> None:
    # /pfabackup is sshfs; /pfa/x must not match it, and /pfabackup/x must not match /pfa.
    assert filesystem_type(Path("/pfabackup/x"), mountinfo=MOUNTINFO) == "fuse.sshfs"
    assert filesystem_type(Path("/pfa/x"), mountinfo=MOUNTINFO) == "ext4"


@needs_posix_paths
def test_octal_escapes_in_mount_points() -> None:
    assert filesystem_type(Path("/mnt/with space/db"), mountinfo=MOUNTINFO) == "cifs"


@needs_posix_paths
def test_network_classification() -> None:
    assert is_network_filesystem(Path("/pfa/documents"), mountinfo=MOUNTINFO)
    assert is_network_filesystem(Path("/mnt/with space"), mountinfo=MOUNTINFO)
    assert is_network_filesystem(Path("/pfabackup"), mountinfo=MOUNTINFO)
    assert not is_network_filesystem(Path("/pfa/data"), mountinfo=MOUNTINFO)


def test_unknown_when_there_is_no_mount_table() -> None:
    assert filesystem_type(Path("/anything"), mountinfo="") is None
    assert not is_network_filesystem(Path("/anything"), mountinfo="")


@pytest.fixture
def netfs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend every directory is on NFS."""
    monkeypatch.setattr(
        "personal_finance_analyzer.db.session.is_network_filesystem", lambda _p: True
    )
    monkeypatch.setattr("personal_finance_analyzer.db.session.filesystem_type", lambda _p: "nfs4")


def test_default_mode_is_wal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(JOURNAL_MODE_ENV, raising=False)
    choice = choose_journal_mode(tmp_path)
    assert (choice.mode, choice.reason) == ("wal", "default")
    assert storage_warning(tmp_path) is None


def test_network_share_falls_back_to_delete_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, netfs: None
) -> None:
    monkeypatch.delenv(JOURNAL_MODE_ENV, raising=False)
    choice = choose_journal_mode(tmp_path)
    assert (choice.mode, choice.reason, choice.fstype) == ("delete", "network-fs", "nfs4")
    warning = storage_warning(tmp_path)
    assert warning is not None
    assert "nfs4" in warning
    assert "local disk" in warning


def test_env_overrides_detection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, netfs: None
) -> None:
    monkeypatch.setenv(JOURNAL_MODE_ENV, "TRUNCATE")
    assert choose_journal_mode(tmp_path).mode == "truncate"
    monkeypatch.setenv(JOURNAL_MODE_ENV, "wal")
    warning = storage_warning(tmp_path)
    assert warning is not None
    assert "can corrupt" in warning


def test_invalid_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JOURNAL_MODE_ENV, "memory")
    with pytest.raises(ValueError, match=JOURNAL_MODE_ENV):
        choose_journal_mode()


@pytest.mark.parametrize(
    ("env", "journal", "synchronous"),
    [("", "wal", 1), ("delete", "delete", 2)],
)
def test_engine_applies_the_chosen_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: str, journal: str, synchronous: int
) -> None:
    from sqlalchemy import text

    if env:
        monkeypatch.setenv(JOURNAL_MODE_ENV, env)
    else:
        monkeypatch.delenv(JOURNAL_MODE_ENV, raising=False)
    engine = make_engine_for_path(tmp_path / "t.db")
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == journal
            assert conn.execute(text("PRAGMA synchronous")).scalar() == synchronous
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()
