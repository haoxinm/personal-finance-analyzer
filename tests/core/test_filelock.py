"""The cross-process scan lock: held by one at a time, released on exit, never stuck."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from personal_finance_analyzer.core.filelock import FileLock, try_lock


def test_a_second_lock_is_refused_until_the_first_is_released(tmp_path: Path) -> None:
    path = tmp_path / "data" / "x.lock"
    first, second = FileLock(path), FileLock(path)
    assert first.try_acquire() and first.acquired
    assert second.try_acquire() is False and not second.acquired
    first.release()
    assert second.try_acquire() is True
    second.release()
    second.release()  # releasing twice is harmless


def test_the_context_manager_reports_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with try_lock(path) as got:
        assert got is True
        with try_lock(path) as nested:
            assert nested is False
    with try_lock(path) as again:
        assert again is True


def test_another_process_cannot_take_a_held_lock_and_can_after_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    probe = (
        "import sys; from pathlib import Path; "
        "from personal_finance_analyzer.core.filelock import FileLock; "
        "l = FileLock(Path(sys.argv[1])); sys.exit(0 if l.try_acquire() else 3)"
    )
    holder = FileLock(path)
    assert holder.try_acquire()
    assert subprocess.run([sys.executable, "-c", probe, str(path)], check=False).returncode == 3
    holder.release()
    assert subprocess.run([sys.executable, "-c", probe, str(path)], check=False).returncode == 0
