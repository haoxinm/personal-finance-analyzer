"""Machine-local state around a backup: the plaintext-key opt-in must not travel, the key file
must survive a restore on the machine that chose it, a freshly migrated empty database must not
demand `--replace`."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from personal_finance_analyzer.backup import archive
from personal_finance_analyzer.core.paths import create_home_layout
from personal_finance_analyzer.db.migrate import upgrade_head

from .support import PASSPHRASE, seed_home

_FILE_KEY = "sk-local-plaintext-key"


def _target(tmp_path: Path) -> Path:
    return tmp_path / "machine_b" / "PersonalFinanceAnalyzer"


def _config(home: Path) -> dict[str, object]:
    return tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))


def test_portable_config_drops_the_secrets_table() -> None:
    raw = b'[llm]\nmodel = "m"\n\n[secrets]\nallow_file_store = true\n'
    out = tomllib.loads(archive._portable_config(raw).decode())
    assert "secrets" not in out
    assert out["llm"] == {"model": "m"}


def test_backup_does_not_carry_the_file_store_opt_in(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    home, _ = source
    with (home / "config.toml").open("a", encoding="utf-8") as fh:
        fh.write("\n[secrets]\nallow_file_store = true\n")
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=PASSPHRASE)

    target = _target(tmp_path)
    archive.restore_backup(backup, passphrase=PASSPHRASE, home=target)
    assert "secrets" not in _config(target)


def test_restore_keeps_this_machines_key_file_and_opt_in(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=PASSPHRASE)

    # Machine B opted into the plaintext key file and saved a key before restoring.
    target = _target(tmp_path)
    seed_home(target)
    (target / "config.toml").write_text("[secrets]\nallow_file_store = true\n", encoding="utf-8")
    (target / "secrets").mkdir(exist_ok=True)
    (target / "secrets" / "api_keys.toml").write_text(
        f'[keys]\n"llm:x" = "{_FILE_KEY}"\n', encoding="utf-8"
    )

    result = archive.restore_backup(backup, passphrase=PASSPHRASE, replace=True, home=target)

    assert result.kept_secrets
    assert _config(target)["secrets"] == {"allow_file_store": True}
    kept = (target / "secrets" / "api_keys.toml").read_text(encoding="utf-8")
    assert _FILE_KEY in kept
    assert result.previous_dir is not None  # the old directory was moved aside, not deleted
    # ...but the key was moved out of it, not copied: no plaintext lingers in *.before-restore.
    assert not (result.previous_dir / "secrets").exists()
    assert (result.previous_dir / "finance.db").is_file()  # the rest of the old data is intact


def test_restore_without_local_secrets_reports_nothing_kept(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=PASSPHRASE)
    target = _target(tmp_path)
    result = archive.restore_backup(backup, passphrase=PASSPHRASE, home=target)
    assert not result.kept_secrets
    assert not (target / "secrets").exists()


def test_restore_over_a_pristine_database_does_not_need_replace(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=PASSPHRASE)
    target = _target(tmp_path)
    create_home_layout(target)
    upgrade_head(f"sqlite:///{target / 'finance.db'}")  # what a bare `pfa migrate` leaves

    archive.restore_backup(backup, passphrase=PASSPHRASE, home=target)  # no replace=True
    assert (target / "finance.db").is_file()


def test_restore_over_a_database_with_data_still_needs_replace(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    from personal_finance_analyzer.backup.errors import RestoreTargetError

    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=PASSPHRASE)
    target = _target(tmp_path)
    seed_home(target)
    (target / "config.toml").unlink()  # only the data decides
    with pytest.raises(RestoreTargetError):
        archive.restore_backup(backup, passphrase=PASSPHRASE, home=target)


def test_restore_leaves_no_plaintext_key_anywhere_in_the_before_restore_copy(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=PASSPHRASE)
    target = _target(tmp_path)
    seed_home(target)
    (target / "secrets").mkdir(exist_ok=True)
    (target / "secrets" / "api_keys.toml").write_text(
        f'[keys]\n"llm:x" = "{_FILE_KEY}"\n', encoding="utf-8"
    )
    result = archive.restore_backup(backup, passphrase=PASSPHRASE, replace=True, home=target)
    assert result.previous_dir is not None
    leaked = [
        p
        for p in result.previous_dir.rglob("*")
        if p.is_file() and _FILE_KEY.encode() in p.read_bytes()
    ]
    assert leaked == []
    assert _FILE_KEY in (target / "secrets" / "api_keys.toml").read_text(encoding="utf-8")


def test_failed_restore_keeps_the_key_file_where_it_was(
    source: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """The key is only removed from the old directory after the swap succeeded."""
    from personal_finance_analyzer.backup.errors import BackupError

    backup = tmp_path / "b.pfabackup"
    archive.create_backup(backup, passphrase=PASSPHRASE)
    target = _target(tmp_path)
    seed_home(target)
    (target / "secrets").mkdir(exist_ok=True)
    key_file = target / "secrets" / "api_keys.toml"
    key_file.write_text(f'[keys]\n"llm:x" = "{_FILE_KEY}"\n', encoding="utf-8")

    def refuse() -> None:
        raise BackupError("busy")

    with pytest.raises(BackupError, match="busy"):
        archive.restore_backup(
            backup, passphrase=PASSPHRASE, replace=True, home=target, before_swap=refuse
        )
    assert _FILE_KEY in key_file.read_text(encoding="utf-8")
    assert [p.name for p in target.parent.iterdir()] == [target.name]


def test_discard_old_secrets_keeps_a_file_whose_copy_is_missing_or_different(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "old"
    target = tmp_path / "new"
    (previous / "secrets").mkdir(parents=True)
    (target / "secrets").mkdir(parents=True)
    (previous / "secrets" / "same.toml").write_text("a", encoding="utf-8")
    (target / "secrets" / "same.toml").write_text("a", encoding="utf-8")
    (previous / "secrets" / "differs.toml").write_text("old", encoding="utf-8")
    (target / "secrets" / "differs.toml").write_text("new", encoding="utf-8")
    (previous / "secrets" / "missing.toml").write_text("only-old", encoding="utf-8")

    archive._discard_old_secrets(previous, target)

    assert not (previous / "secrets" / "same.toml").exists()
    assert (previous / "secrets" / "differs.toml").read_text(encoding="utf-8") == "old"
    assert (previous / "secrets" / "missing.toml").read_text(encoding="utf-8") == "only-old"
