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
