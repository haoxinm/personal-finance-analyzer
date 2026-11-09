"""A non-blocking, cross-process file lock (one scan of the documents folder at a time).

The folder scan (`watch/`) runs inside `pfa serve` and from `pfa documents scan`; two servers on the
same data directory, or a server and a command, must never import the same files at once. The lock
is held only while a scan runs and is released by the operating system if the process dies, so a
crash can never leave it stuck. It is an advisory lock on a small file in the data directory
(``flock`` on POSIX, ``msvcrt.locking`` on Windows); different `FileLock` objects in one process
exclude each other too.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType


class FileLock:
    """``with FileLock(path) as lock: if lock.acquired: ...`` or `try_acquire` / `release`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Take the lock if nobody holds it; never waits. ``False`` when it is held elsewhere."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(fd)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            _unlock(fd)
        os.close(fd)

    def __enter__(self) -> FileLock:
        self.try_acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


@contextlib.contextmanager
def try_lock(path: Path) -> Iterator[bool]:
    """Yield whether the lock at ``path`` was taken; it is released on exit."""
    lock = FileLock(path)
    try:
        yield lock.try_acquire()
    finally:
        lock.release()


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
