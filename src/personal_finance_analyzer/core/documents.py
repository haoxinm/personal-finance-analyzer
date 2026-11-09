"""The documents directory on disk: placing, finding and removing the readable PDF copies.

This module knows nothing about the database (`db/documents.py` decides *what* a row's file is
called; `core/doc_names.py` how names are built). It provides the primitives every writer shares:

- `place_file` copies a PDF into ``<documents>/<folder>/<stem>.pdf`` and returns the path relative
  to the documents directory (what the database stores). The copy is crash-safe (written to a
  hidden temp file next to the destination, fsynced, then published without ever overwriting) and
  collision-safe (an existing file with other content gets `` (2)``, `` (3)``..., one with the
  *same* content is reused, which also makes a repeated or resumed run idempotent);
- `kept_pdf` finds the file of a row: by its recorded relative path, else by content hash anywhere
  in the folder (so a PDF the user moved or renamed by hand is still found; see `find_by_sha`);
- `discard` removes a file and the folders it leaves empty.

Permissions: on a normal install directories are created 0700 and files 0600. In a container the
entrypoint's ``UMASK`` decides instead (the point of a NAS share is that the NAS user, and perhaps
a group, can open the files), and a restore or reorganize keeps whatever mode the file had. An
existing directory (the mount point, a share's root, a folder the user made) is never chmod-ed.

The folder may hold anything the user puts there; hidden entries (leading dot, which includes this
module's temp files) are ignored, symlinks are never followed, and nothing but our own files is
ever deleted.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath

from personal_finance_analyzer.core import doc_names
from personal_finance_analyzer.core.container import running_in_container
from personal_finance_analyzer.core.errors import PfaError
from personal_finance_analyzer.core.paths import documents_dir, legacy_statements_dir, staging_dir

_log = logging.getLogger(__name__)

README_NAME = "README.txt"
_CHUNK = 1024 * 1024
_MAX_DEPTH = 8

README_TEXT = """\
Personal Finance Analyzer - your documents
==========================================

This folder holds the PDF statements and tax forms you imported, under names you can read:

  <Person>/Statements/<Bank>/<YYYY-MM> <Bank> <account>.pdf     bank and card statements
  <Person>/Tax/<year>/<Form> - <Payer>.pdf                       W-2, 1099 and 1040 forms
  Shared/                                                        files not assigned to a person

You can open, copy and back up these files without the app. If you move, rename or delete one, the
app still finds a moved or renamed file by its content (it just shows "PDF not found" for one you
deleted). The app does NOT rename files by itself when you rename a person or an account; run
`pfa documents reorganize` (it can show a --dry-run first) to apply the current names.

Adding files yourself: the folders say whose a PDF is and what it is, so you can also drop PDFs
here by hand (from any computer, over the network share) and the app imports them:

  <Person>/Statements/<anything>/<any name>.pdf     a statement for that person
  <Person>/Tax/<year>/<any name>.pdf                a tax form for that person

<Person> must be the name of a person in the app (capitals and spacing do not matter). Turn
"Folder imports" on in the app's Settings (or run `pfa documents scan` once). A file stays exactly
where you put it; one the app cannot import (unknown person, a PDF with a password, a statement
layout it does not know yet) is listed on its Import page with the reason, never silently skipped.
Files that sit anywhere else, such as directly in this folder, are not imported.

The database, settings and everything else the app needs live in its data folder, not here (in
Docker: ./pfa/data next to ./pfa/documents). The app does not need this README; it recreates it
only if it is missing, and never overwrites your edits.

These files are sensitive (account numbers, income, sometimes full SSNs). Keep this folder private
and protect any network share it is on.
"""


class DocumentsError(PfaError):
    """A copy into the documents directory could not be completed (content mismatch, no space)."""


# --------------------------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------------------------


def _read_umask() -> int:
    old = os.umask(0)
    os.umask(old)
    return old


#: The process umask, read once at import (before request threads exist): in a container it is the
#: entrypoint's `UMASK`.
_UMASK = _read_umask()


def _strict_permissions() -> bool:
    """Dirs 0700 / files 0600 (a normal install) as opposed to "whatever UMASK says" (a container)."""
    return not running_in_container()


def _chmod_new(path: Path, mode: int) -> None:
    with contextlib.suppress(OSError):  # not every filesystem has permission bits
        path.chmod(mode)


def make_dirs(directory: Path, *, within: Path | None = None) -> None:
    """Create ``directory`` and its missing parents; only the directories created here get the
    private mode (existing ones, such as a mount point, are left alone). ``within`` is the
    directory above which nothing is created: creating it is `ensure_documents_dir`'s business."""
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        if current.parent == current or (within is not None and current == within):
            break
        current = current.parent
    for path in reversed(missing):
        try:
            path.mkdir()
        except FileExistsError:
            continue
        if _strict_permissions():
            _chmod_new(path, 0o700)
        else:
            _chmod_new(path, 0o777 & ~_UMASK)


def ensure_documents_dir(root: Path | None = None) -> Path:
    """Create the documents directory (and its README.txt, if absent) and return it. ``root``
    defaults to the configured one (`core.paths.documents_dir`)."""
    root = root if root is not None else documents_dir()
    make_dirs(root)
    readme = root / README_NAME
    if not readme.exists():
        with contextlib.suppress(OSError):
            _write_new(readme, README_TEXT.encode("utf-8"))
    return root


def file_mode() -> int:
    return 0o600 if _strict_permissions() else 0o666  # the container's umask trims the latter


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, file_mode())
    with os.fdopen(fd, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


# --------------------------------------------------------------------------------------------
# Hashing and the content index
# --------------------------------------------------------------------------------------------

_INDEX_LOCK = threading.Lock()
#: path -> (size, mtime_ns, sha256): a file is re-hashed only when it changed on disk.
_HASHES: dict[str, tuple[int, int, str]] = {}


def file_sha256(path: Path) -> str | None:
    """SHA-256 of ``path`` (cached by size and mtime), or None if it cannot be read. The lock is
    held only for the cache lookups, never while the file is being read."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    with _INDEX_LOCK:
        cached = _HASHES.get(key)
    if cached is not None and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
        return cached[2]
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    with _INDEX_LOCK:
        _HASHES[key] = (stat.st_size, stat.st_mtime_ns, digest.hexdigest())
    return digest.hexdigest()


def _forget(path: Path) -> None:
    with _INDEX_LOCK:
        _HASHES.pop(str(path), None)


def invalidate_index() -> None:
    """Forget every cached hash (tests; a restore that replaced the folder's contents)."""
    with _INDEX_LOCK:
        _HASHES.clear()


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def iter_files(root: Path | None = None, *, pdf_only: bool = True) -> Iterator[tuple[str, Path]]:
    """Every regular, visible file under the documents directory as ``(relative posix path,
    absolute path)``, in a stable order. Hidden entries and symlinks are skipped, and so is the
    README at the root."""
    base = root if root is not None else documents_dir()
    if not base.is_dir():
        return
    stack: list[tuple[Path, int]] = [(base, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        subdirs: list[tuple[Path, int]] = []
        for entry in entries:
            if _is_hidden(entry.name):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth < _MAX_DEPTH:
                        subdirs.append((Path(entry.path), depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if directory == base and entry.name == README_NAME:
                continue
            if pdf_only and not entry.name.casefold().endswith(doc_names.PDF_SUFFIX):
                continue
            path = Path(entry.path)
            yield PurePosixPath(*path.relative_to(base).parts).as_posix(), path
        stack.extend(reversed(subdirs))


def find_by_sha(sha256: str, root: Path | None = None) -> Path | None:
    """A PDF under the documents directory whose content hash is ``sha256``, wherever the user
    put it (``None`` when there is none). Hashes each file once per process (and again only when
    it changes), so the first miss on a big folder is slow (on a network share, very) and later
    ones are a directory walk. Walks and hashes *without* holding the index lock, so reading or
    placing other files is never blocked behind it. Call it on demand (opening one PDF, `check`,
    `reorganize`), never per row of a list; use `ShaIndex` to look several hashes up in one pass."""
    base = root if root is not None else documents_dir()
    for _rel, path in iter_files(base):
        if file_sha256(path) == sha256:
            return path
    return None


def known_by_sha(sha256: str, root: Path | None = None) -> Path | None:
    """Like `find_by_sha`, but only consults hashes already computed (no walking, no hashing, one
    ``stat`` per candidate): what a list view can afford. A file the user moved is found only if
    something has hashed it before (opening it, `pfa documents check`); otherwise ``None``."""
    base = str(root if root is not None else documents_dir())
    with _INDEX_LOCK:
        candidates = [
            (key, size, mtime)
            for key, (size, mtime, digest) in _HASHES.items()
            if digest == sha256 and key.startswith(base)
        ]
    for key, size, mtime in candidates:
        try:
            stat = os.stat(key)
        except OSError:
            continue
        if stat.st_size == size and stat.st_mtime_ns == mtime:
            return Path(key)
    return None


class ShaIndex:
    """Content hash -> file for the whole documents folder, built once on first use (a walk plus
    the hashes not cached yet) and then used for any number of lookups. Built without holding the
    module's lock; use one per operation, not as a long-lived cache."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else documents_dir()
        self._by_sha: dict[str, Path] | None = None

    def find(self, sha256: str) -> Path | None:
        if self._by_sha is None:
            built: dict[str, Path] = {}
            for _rel, path in iter_files(self._root):
                digest = file_sha256(path)
                if digest is not None:
                    built.setdefault(digest, path)
            self._by_sha = built
        return self._by_sha.get(sha256)


# --------------------------------------------------------------------------------------------
# Resolving a recorded path
# --------------------------------------------------------------------------------------------


def resolve(stored_path: str, root: Path | None = None) -> Path | None:
    """The absolute path a recorded ``stored_path`` names. A *relative* path (the current format)
    is looked up under the documents directory and may not climb out of it (``None`` for ``..``
    or an empty path); an *absolute* one (written before documents had their own folder) is
    returned as is."""
    if not stored_path:
        return None
    raw = Path(stored_path)
    if raw.is_absolute():
        return raw
    parts = PurePosixPath(stored_path.replace("\\", "/")).parts
    if not parts or any(p in ("..", "") or p.startswith("/") for p in parts):
        return None
    return (root if root is not None else documents_dir()).joinpath(*parts)


def recorded_pdf(stored_path: str | None, root: Path | None = None) -> Path | None:
    """The file at the recorded path, if it is there: one ``stat``, never a search."""
    if not stored_path:
        return None
    recorded = resolve(stored_path, root)
    return recorded if recorded is not None and recorded.is_file() else None


def kept_pdf(
    stored_path: str | None,
    file_sha256_hex: str,
    root: Path | None = None,
    *,
    search: bool = True,
) -> Path | None:
    """The kept copy of an imported PDF (a statement, or a tax document), or ``None`` if there is
    none on disk. Only rows that recorded a copy (``stored_path``) have one.

    Looked up by the recorded path first; if the file is not there any more (the user moved or
    renamed it, the folder was restored elsewhere) by content hash across the documents directory;
    and, for a row not yet carried over by the upgrade, in the old ``statements/`` folder.

    ``search=False`` is for list views: it never walks or hashes the folder (a network share may
    take minutes), it only uses what is already known (`known_by_sha`)."""
    if not stored_path:
        return None
    recorded = recorded_pdf(stored_path, root)
    if recorded is not None:
        return recorded
    found = find_by_sha(file_sha256_hex, root) if search else known_by_sha(file_sha256_hex, root)
    if found is not None:
        return found
    legacy = legacy_statements_dir() / f"{file_sha256_hex}.pdf"
    return legacy if legacy.is_file() else None


def staged_pdf(file_sha256_hex: str) -> Path | None:
    """The upload waiting in ``incoming/`` (or, before the upgrade ran, in ``statements/``)."""
    for base in (staging_dir(), legacy_statements_dir()):
        candidate = base / f"{file_sha256_hex}.pdf"
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------------------------
# Placing a file
# --------------------------------------------------------------------------------------------


def _sync_dir(directory: Path) -> None:
    if os.name == "nt":  # pragma: no cover - cannot fsync a directory handle on Windows
        return
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _copy_to_temp(src: Path, directory: Path, expected_sha: str | None) -> Path:
    """Write ``src`` to a hidden temp file in ``directory`` (fsynced); verify its hash if given."""
    tmp = directory / f".{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, file_mode())
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if expected_sha is not None and digest.hexdigest() != expected_sha:
        tmp.unlink(missing_ok=True)
        raise DocumentsError("the PDF changed while it was being copied (hash mismatch)")
    return tmp


def publish_no_clobber(tmp: Path, final: Path) -> None:
    """Move ``tmp`` to ``final`` unless ``final`` exists (raises `FileExistsError` then). Uses a
    hard link, which is atomic and never overwrites; filesystems without hard links (SMB, FAT)
    fall back to a check followed by a rename."""
    try:
        os.link(tmp, final)
    except FileExistsError:
        raise
    except OSError:
        if final.exists():
            raise FileExistsError(str(final)) from None
        os.replace(tmp, final)
        return
    tmp.unlink(missing_ok=True)


def place_file(
    src: Path,
    folder: str,
    stem: str,
    sha256: str,
    *,
    claimed: Callable[[str], bool] | None = None,
    root: Path | None = None,
) -> str:
    """Copy ``src`` (a PDF whose content hash is ``sha256``) into the documents directory as
    ``<folder>/<stem>.pdf`` and return that path relative to it (``/``-separated).

    A name that is taken by a file with *other* content, or that ``claimed`` says another record
    owns, becomes ``<stem> (2).pdf``, ``<stem> (3).pdf``...; a name taken by a file with the same
    content is returned as is (nothing is copied), so running this twice, or after a crash that
    left the copy behind, is a no-op. Names are compared case-insensitively (macOS and Windows
    folders are). ``src`` is never modified or removed.

    Raises:
        DocumentsError: the content of ``src`` does not hash to ``sha256``.
        OSError: the folder cannot be written."""
    root = root if root is not None else documents_dir()
    directory = root.joinpath(*PurePosixPath(folder).parts)
    make_dirs(directory, within=root)
    tmp: Path | None = None
    attempt = 1
    try:
        while True:
            name = doc_names.file_name(stem, doc_names.collision_suffix(attempt))
            rel = f"{folder}/{name}"
            try:
                listing = {p.name.casefold(): p.name for p in directory.iterdir()}
            except OSError:
                listing = {}
            existing = listing.get(name.casefold())
            if existing is not None:
                if file_sha256(directory / existing) == sha256:
                    return f"{folder}/{existing}"
                attempt += 1
                continue
            if claimed is not None and claimed(rel):
                attempt += 1
                continue
            if tmp is None:
                tmp = _copy_to_temp(src, directory, sha256)
            try:
                publish_no_clobber(tmp, directory / name)
            except FileExistsError:
                continue  # lost a race for this name: look at it again (it may be our content)
            _forget(directory / name)
            _sync_dir(directory)
            return rel
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def rename_within(
    old: Path,
    folder: str,
    stem: str,
    sha256: str,
    *,
    claimed: Callable[[str], bool] | None = None,
    root: Path | None = None,
) -> str:
    """Rename ``old`` (a file already under the documents directory) to ``<folder>/<stem>.pdf``
    with the same collision rules as `place_file`, keeping its permissions; returns the new
    relative path. If ``old`` already is that file nothing happens."""
    root = root if root is not None else documents_dir()
    directory = root.joinpath(*PurePosixPath(folder).parts)
    make_dirs(directory, within=root)
    attempt = 1
    while True:
        name = doc_names.file_name(stem, doc_names.collision_suffix(attempt))
        rel = f"{folder}/{name}"
        target = directory / name
        listing = {p.name.casefold(): p.name for p in directory.iterdir()}
        existing = listing.get(name.casefold())
        if existing is not None:
            current = directory / existing
            if _same_file(current, old):
                return f"{folder}/{existing}"  # already named so (letter case aside)
            if file_sha256(current) == sha256:
                # An identical copy already sits there: it becomes the record's file, and the
                # duplicate at the old place is removed, but only if it really is that content.
                if file_sha256(old) == sha256:
                    discard_path(old, root)
                return f"{folder}/{existing}"
            attempt += 1
            continue
        if claimed is not None and claimed(rel):
            attempt += 1
            continue
        try:
            publish_no_clobber_rename(old, target)
        except FileExistsError:
            continue  # lost a race for this name: look at it again
        _sync_dir(directory)
        return rel


def publish_no_clobber_rename(old: Path, new: Path) -> None:
    """Rename ``old`` to ``new`` without ever replacing ``new`` (hard link + unlink; a plain
    check-then-rename where hard links are unavailable). Across filesystems it copies instead."""
    try:
        os.link(old, new)
    except FileExistsError:
        raise
    except OSError:
        if new.exists():
            raise FileExistsError(str(new)) from None
        try:
            os.replace(old, new)
        except OSError:
            tmp = _copy_to_temp(old, new.parent, None)
            try:
                publish_no_clobber(tmp, new)
            finally:
                tmp.unlink(missing_ok=True)
            old.unlink(missing_ok=True)
        return
    old.unlink(missing_ok=True)
    _forget(old)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


# --------------------------------------------------------------------------------------------
# Removing
# --------------------------------------------------------------------------------------------


def discard_path(path: Path, root: Path | None = None) -> bool:
    """Delete ``path`` and prune the folders it leaves empty (never the documents directory
    itself). Returns whether a file was removed."""
    try:
        path.unlink()
    except FileNotFoundError:
        removed = False
    except OSError as exc:
        _log.warning("could not delete %s (%s)", path, exc)
        removed = False
    else:
        removed = True
    _forget(path)
    prune_empty_dirs(path.parent, root)
    return removed


#: Folders at this depth or shallower below the documents directory (``<Person>``,
#: ``<Person>/Statements``, ``<Person>/Tax``) are where the user drops files: never pruned.
_KEEP_DEPTH = 2


def prune_empty_dirs(directory: Path, root: Path | None = None) -> None:
    """Remove ``directory`` and its parents while they are empty and inside the documents
    directory, but only the folders *below* ``<Person>/Statements`` and ``<Person>/Tax`` (an
    institution or a tax year): the documents directory, the person folders and their
    ``Statements``/``Tax`` folders are the user's drop folders and stay even when empty."""
    root = root if root is not None else documents_dir()
    current = directory
    while current != root and root in current.parents:
        if len(current.relative_to(root).parts) <= _KEEP_DEPTH:
            return
        try:
            current.rmdir()
        except OSError:
            return  # not empty (or not ours to remove)
        current = current.parent
