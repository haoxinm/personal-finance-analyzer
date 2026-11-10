"""The suite-wide guard against reaching the developer's real data directory or keychain."""

from __future__ import annotations

import os
from pathlib import Path

import keyring
import pytest

from tests import conftest


def test_process_is_pointed_at_scratch_directories() -> None:
    conftest._assert_isolated()
    assert Path(os.environ["HOME"]).resolve() not in {d.resolve() for d in conftest._REAL_DATA_DIRS}
    assert os.environ["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"


def test_the_keyring_is_the_in_memory_fake() -> None:
    backend = keyring.get_keyring()
    assert type(backend).__name__ == "_MemoryKeyring"
    assert not conftest._is_real_keyring(backend)


@pytest.mark.parametrize("name", ["PFA_HOME", "HOME"])
def test_guard_rejects_a_real_or_unset_location(
    monkeypatch: pytest.MonkeyPatch, name: str, tmp_path: Path
) -> None:
    monkeypatch.delenv(name)
    with pytest.raises(AssertionError, match=name):
        conftest._assert_isolated()


def test_guard_rejects_pfa_home_inside_a_real_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    real = conftest._REAL_DATA_DIRS[0]
    monkeypatch.setenv("PFA_HOME", str(real))
    with pytest.raises(AssertionError):
        conftest._assert_isolated()


def test_real_backends_are_recognised() -> None:
    from keyring.backends import fail, null

    assert not conftest._is_real_keyring(fail.Keyring())
    assert not conftest._is_real_keyring(null.Keyring())

    class Fake:
        __module__ = "keyring.backends.macOS"

    assert conftest._is_real_keyring(Fake())


def test_snapshot_detects_changes_and_is_read_only(tmp_path: Path) -> None:
    assert conftest._snapshot(tmp_path / "absent") is None
    root = tmp_path / "data"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.txt").write_text("one")
    before = conftest._snapshot(root)
    assert before is not None
    assert conftest._snapshot(root) == before  # taking a snapshot changes nothing

    (root / "sub" / "f.txt").write_text("two-longer")
    assert conftest._snapshot(root) != before
    before = conftest._snapshot(root)
    (root / "new.db").write_text("x")
    assert conftest._snapshot(root) != before


def test_session_finish_fails_the_run_when_a_real_dir_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from types import SimpleNamespace

    watched = tmp_path / "SpendAnalyzer"
    watched.mkdir()
    (watched / "spend.db").write_text("data")
    monkeypatch.setattr(
        conftest, "_REAL_DIR_SNAPSHOTS", {watched: conftest._snapshot(watched)}, raising=True
    )
    session = SimpleNamespace(
        config=SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda name: None)),
        exitstatus=0,
    )

    conftest.pytest_sessionfinish(session, 0)  # type: ignore[arg-type]
    assert session.exitstatus == 0  # untouched: nothing to report

    (watched / "spend.db").write_text("changed by a test")
    conftest.pytest_sessionfinish(session, 0)  # type: ignore[arg-type]
    assert session.exitstatus == 1
    assert "TEST ISOLATION VIOLATION" in capsys.readouterr().err
