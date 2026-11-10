from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_finance_analyzer.cli import app
from personal_finance_analyzer.core.paths import PFA_HOME_ENV
from tests.backup.support import DESCRIPTION, PASSPHRASE, USER_NAME, seed_home

runner = CliRunner()


@pytest.fixture
def pass_file(tmp_path: Path) -> Path:
    path = tmp_path / "pass.txt"
    path.write_text(PASSPHRASE + "\n", encoding="utf-8")
    return path


@pytest.fixture
def seeded(home: Path) -> Path:
    seed_home(home)
    return home


def _use_home(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv(PFA_HOME_ENV, str(path))


def test_create_inspect_restore_round_trip(
    seeded: Path, tmp_path: Path, pass_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "all.pfabackup"
    result = runner.invoke(app, ["backup", "create", str(out), "--passphrase-file", str(pass_file)])
    assert result.exit_code == 0, result.output
    assert "Backup written to" in result.output
    assert "1 transactions" in result.output
    assert "API key" in result.output  # says the keychain key is not included
    assert "NOT encrypted" not in result.output

    inspected = runner.invoke(
        app, ["backup", "inspect", str(out), "--passphrase-file", str(pass_file)]
    )
    assert inspected.exit_code == 0, inspected.output
    assert "Encrypted:  yes" in inspected.output
    assert "transactions" in inspected.output
    assert "statement PDFs" in inspected.output
    # Counts and dates only: nothing personal comes out of `inspect`.
    for personal in (USER_NAME, DESCRIPTION, "Quillfeather", "Fixture Bank"):
        assert personal not in inspected.output

    other = tmp_path / "machine_b" / "PersonalFinanceAnalyzer"
    _use_home(monkeypatch, other)
    restored = runner.invoke(
        app, ["backup", "restore", str(out), "--passphrase-file", str(pass_file)]
    )
    assert restored.exit_code == 0, restored.output
    assert "Restored backup" in restored.output
    assert "llm set-key" in restored.output
    assert (other / "finance.db").is_file()

    users = runner.invoke(app, ["users", "list"])
    assert USER_NAME in users.output


def test_create_into_directory_names_a_dated_file(
    seeded: Path, tmp_path: Path, pass_file: Path
) -> None:
    folder = tmp_path / "usb"
    folder.mkdir()
    result = runner.invoke(
        app, ["backup", "create", str(folder), "--passphrase-file", str(pass_file)]
    )
    assert result.exit_code == 0, result.output
    assert [p.suffix for p in folder.iterdir()] == [".pfabackup"]


def test_create_prompts_for_passphrase_with_confirmation(seeded: Path, tmp_path: Path) -> None:
    out = tmp_path / "prompted.pfabackup"
    result = runner.invoke(
        app, ["backup", "create", str(out)], input=f"{PASSPHRASE}\n{PASSPHRASE}\n"
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    restored_inspect = runner.invoke(app, ["backup", "inspect", str(out)], input=f"{PASSPHRASE}\n")
    assert restored_inspect.exit_code == 0, restored_inspect.output
    assert "Encrypted:  yes" in restored_inspect.output


def test_no_encrypt_is_explicit_and_warns(seeded: Path, tmp_path: Path) -> None:
    out = tmp_path / "plain.pfabackup"
    result = runner.invoke(app, ["backup", "create", str(out), "--no-encrypt"])
    assert result.exit_code == 0, result.output
    assert "NOT encrypted" in result.output
    inspected = runner.invoke(app, ["backup", "inspect", str(out)])
    assert inspected.exit_code == 0, inspected.output  # no passphrase asked for
    assert "Encrypted:  no" in inspected.output


def test_no_encrypt_with_passphrase_file_is_a_usage_error(
    seeded: Path, tmp_path: Path, pass_file: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "backup",
            "create",
            str(tmp_path / "x.pfabackup"),
            "--no-encrypt",
            "--passphrase-file",
            str(pass_file),
        ],
    )
    assert result.exit_code == 1
    assert "cannot be used together" in result.output


def test_create_refuses_to_overwrite_without_force(
    seeded: Path, tmp_path: Path, pass_file: Path
) -> None:
    out = tmp_path / "all.pfabackup"
    args = ["backup", "create", str(out), "--passphrase-file", str(pass_file)]
    assert runner.invoke(app, args).exit_code == 0
    again = runner.invoke(app, args)
    assert again.exit_code == 1
    assert "--force" in again.output
    assert runner.invoke(app, [*args, "--force"]).exit_code == 0


def test_short_passphrase_file_is_rejected(seeded: Path, tmp_path: Path) -> None:
    short = tmp_path / "short.txt"
    short.write_text("abc\n", encoding="utf-8")
    result = runner.invoke(
        app, ["backup", "create", str(tmp_path / "x.pfabackup"), "--passphrase-file", str(short)]
    )
    assert result.exit_code == 1
    assert "at least 8 characters" in result.output


def test_restore_into_existing_data_needs_replace_and_keeps_old_dir(
    seeded: Path, tmp_path: Path, pass_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "all.pfabackup"
    assert (
        runner.invoke(
            app, ["backup", "create", str(out), "--passphrase-file", str(pass_file)]
        ).exit_code
        == 0
    )
    other = tmp_path / "machine_b" / "PersonalFinanceAnalyzer"
    seed_home(other)
    _use_home(monkeypatch, other)

    refused = runner.invoke(
        app, ["backup", "restore", str(out), "--passphrase-file", str(pass_file)]
    )
    assert refused.exit_code == 1
    assert "--replace" in refused.output

    ok = runner.invoke(
        app, ["backup", "restore", str(out), "--passphrase-file", str(pass_file), "--replace"]
    )
    assert ok.exit_code == 0, ok.output
    assert "previous data directory was kept at" in ok.output
    assert [p.name for p in other.parent.glob("*.before-restore")]


def test_wrong_passphrase_exits_nonzero_with_message(
    seeded: Path, tmp_path: Path, pass_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "all.pfabackup"
    runner.invoke(app, ["backup", "create", str(out), "--passphrase-file", str(pass_file)])
    wrong = tmp_path / "wrong.txt"
    wrong.write_text("definitely the wrong one\n", encoding="utf-8")
    other = tmp_path / "machine_b" / "PersonalFinanceAnalyzer"
    _use_home(monkeypatch, other)
    result = runner.invoke(app, ["backup", "restore", str(out), "--passphrase-file", str(wrong)])
    assert result.exit_code == 1
    assert "wrong passphrase" in result.output
    assert not other.exists()


def test_missing_file_and_missing_database(tmp_path: Path, home: Path) -> None:
    missing = runner.invoke(app, ["backup", "restore", str(tmp_path / "nope.pfabackup")])
    assert missing.exit_code == 1
    assert "no such file" in missing.output
    nothing = runner.invoke(
        app, ["backup", "create", str(tmp_path / "x.pfabackup"), "--no-encrypt"]
    )
    assert nothing.exit_code == 1
    assert "nothing to back up" in nothing.output
