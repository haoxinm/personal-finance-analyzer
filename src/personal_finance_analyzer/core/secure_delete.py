"""Best-effort removal of files that hold a secret (the plaintext API key file).

Overwriting a file before unlinking it stops a plain undelete or a look at the freed blocks from
recovering it on a filesystem that updates in place. It is not a guarantee: a copy-on-write
filesystem (APFS, btrfs, ZFS), an SSD's wear levelling, a snapshot or a backup tool may keep older
blocks. The real protection is not leaving the key on disk at all (use the OS keychain); this only
makes the removal of a plaintext copy as thorough as a user-space program can.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

_CHUNK = 64 * 1024


def shred_file(path: Path) -> None:
    """Overwrite ``path`` with zeros, flush it to disk and unlink it. A missing file is fine; a
    symlink is unlinked without touching its target. Never raises for the overwrite itself (the
    unlink is what matters); an unlink failure propagates as `OSError`."""
    try:
        if path.is_symlink() or not path.is_file():
            path.unlink(missing_ok=True)
            return
        size = path.stat().st_size
        with contextlib.suppress(OSError), path.open("r+b") as fh:
            remaining = size
            zeros = bytes(min(_CHUNK, size)) if size else b""
            while remaining > 0:
                step = min(len(zeros), remaining)
                fh.write(zeros[:step])
                remaining -= step
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        path.unlink(missing_ok=True)


def shred_tree(root: Path) -> None:
    """`shred_file` every file under ``root``, then remove the directories (and ``root``)."""
    if not root.exists() and not root.is_symlink():
        return
    if root.is_symlink() or root.is_file():
        shred_file(root)
        return
    for path in sorted(root.rglob("*"), reverse=True):  # children before their directory
        if path.is_dir() and not path.is_symlink():
            with contextlib.suppress(OSError):
                path.rmdir()
        else:
            shred_file(path)
    root.rmdir()
