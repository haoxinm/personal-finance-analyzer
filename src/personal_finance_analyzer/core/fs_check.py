"""Which kind of filesystem a directory lives on (Linux only), to keep SQLite off network shares.

SQLite's write-ahead log needs shared memory and byte-range locks that NFS, SMB/CIFS and similar
network or FUSE filesystems do not provide reliably; a database in WAL mode on one can corrupt
silently. The data directory of a NAS install is often a bind mount of exactly such a share, so
`pfa serve` checks, and `db/session.py` falls back to the rollback journal when it finds one.

Detection reads ``/proc/self/mountinfo`` (the longest mount point that is a prefix of the path
wins; inside a container that is the bind mount's real filesystem type). Anywhere without that
file (macOS, Windows) the type is unknown and nothing is flagged.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Filesystem types that do not support SQLite's WAL safely: network filesystems and the FUSE
#: layers that wrap them (or the host share of a desktop Docker VM).
NETWORK_FS_TYPES = frozenset(
    {
        "nfs",
        "nfs3",
        "nfs4",
        "cifs",
        "smb",
        "smb2",
        "smb3",
        "smbfs",
        "afs",
        "ncpfs",
        "9p",
        "ceph",
        "glusterfs",
        "lustre",
        "beegfs",
        "davfs",
        "afpfs",
        "fuse.sshfs",
        "fuse.rclone",
        "fuse.s3fs",
        "fuse.davfs",
        "fuse.glusterfs",
        "fuse.grpcfuse",
        "fuse.osxfs",
        "fuse.mergerfs",
        "fuse.shfs",  # Unraid's /mnt/user union filesystem
    }
)

_MOUNTINFO = Path("/proc/self/mountinfo")
_ESCAPE = re.compile(r"\\([0-7]{3})")


def _unescape(field: str) -> str:
    """mountinfo writes space, tab, newline and backslash as three-digit octal escapes."""
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def filesystem_type(path: Path, *, mountinfo: str | None = None) -> str | None:
    """The filesystem type (``ext4``, ``nfs4``, ``cifs``, ...) holding ``path``, or None when it
    cannot be determined. Pure read. ``mountinfo`` is the file's text (for tests)."""
    if mountinfo is None:
        try:
            mountinfo = _MOUNTINFO.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    try:
        target = path.resolve().as_posix()
    except OSError:
        return None
    best_len = -1
    best_type: str | None = None
    for line in mountinfo.splitlines():
        head, sep, tail = line.partition(" - ")
        if not sep:
            continue
        head_fields = head.split()
        tail_fields = tail.split()
        if len(head_fields) < 5 or not tail_fields:
            continue
        mount_point = _unescape(head_fields[4]).rstrip("/") or "/"
        under = mount_point == "/" or target == mount_point or target.startswith(mount_point + "/")
        if under and len(mount_point) > best_len:
            best_len = len(mount_point)
            best_type = tail_fields[0]
    return best_type


def is_network_filesystem(path: Path, *, mountinfo: str | None = None) -> bool:
    """Whether ``path`` is on a filesystem in `NETWORK_FS_TYPES`."""
    fstype = filesystem_type(path, mountinfo=mountinfo)
    return fstype is not None and fstype.lower() in NETWORK_FS_TYPES
