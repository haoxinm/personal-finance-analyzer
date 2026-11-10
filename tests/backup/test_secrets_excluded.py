"""The opt-in plaintext key file (`<home>/secrets/api_keys.toml`) must never travel in a backup."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from personal_finance_analyzer import secret_store
from personal_finance_analyzer.backup import archive
from personal_finance_analyzer.backup.errors import BackupFormatError

from .test_archive import _rewrite_zip

_FILE_KEY = "sk-file-store-key-must-never-be-archived"


def test_backup_excludes_the_secrets_directory(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    home, _ = source
    secret_store.write_file_key("llm:llm.example.com", _FILE_KEY)
    assert (home / "secrets" / "api_keys.toml").is_file()  # it really is in the data directory

    backup = tmp_path / "plain.pfabackup"
    archive.create_backup(backup, passphrase=None)

    with zipfile.ZipFile(backup) as zf:
        names = zf.namelist()
        everything = b"".join(zf.read(n) for n in names)
    assert not [n for n in names if "secret" in n or "api_keys" in n]
    assert _FILE_KEY.encode() not in everything


def test_encrypted_backup_does_not_contain_it_either(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    secret_store.write_file_key("llm:llm.example.com", _FILE_KEY)
    backup = tmp_path / "enc.pfabackup"
    archive.create_backup(backup, passphrase="correct horse battery staple")
    info = archive.inspect_backup(backup, passphrase="correct horse battery staple")
    assert not [f.path for f in info.manifest.files if "secret" in f.path or "api_keys" in f.path]


@pytest.mark.parametrize("member", ["secrets/api_keys.toml", "secrets/other.toml", "api_keys.toml"])
def test_a_crafted_archive_carrying_the_key_file_is_rejected(
    source: tuple[Path, dict[str, str]], tmp_path: Path, member: str
) -> None:
    backup = tmp_path / "plain.pfabackup"
    archive.create_backup(backup, passphrase=None)
    crafted = tmp_path / "crafted.pfabackup"
    _rewrite_zip(backup, crafted, {member: b'[keys]\n"llm:x" = "planted"\n'})
    with pytest.raises(BackupFormatError):
        archive.inspect_backup(crafted)
    target = tmp_path / "machine_b" / "PersonalFinanceAnalyzer"
    with pytest.raises(BackupFormatError):
        archive.restore_backup(crafted, passphrase=None, home=target)
    assert not (target / "secrets").exists()
