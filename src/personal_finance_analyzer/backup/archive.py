"""Create, inspect and restore a `.pfabackup` archive (export -> transfer -> import).

Format (version 1): a zip file, optionally wrapped in `crypto.py`'s passphrase encryption, holding

- ``manifest.json``: format name/version, app version, alembic schema revision, creation time,
  row counts (no PII) and every other member's path, size and SHA-256;
- ``db/finance.db``: a consistent snapshot taken with SQLite's online backup API, so it is safe to
  take while ``pfa serve`` is running;
- ``documents/<relative path>.pdf``: the documents folder (the kept PDFs under their readable
  names, ``documents/Alex/Statements/Chase/2024-03 Chase credit 1234.pdf``); only PDFs, and only
  names that are portable (a file whose name Windows could not hold is left out and reported);
- ``incoming/<sha256>.pdf``: uploads still waiting for their extractor to be confirmed;
- ``statements/<sha256>.pdf``: what older versions called the kept PDF copies; still accepted when
  restoring, and written for any PDF the startup upgrade has not carried over yet;
- ``specs/...``: layout-spec mirrors, and ``config.toml``.

Deliberately **not** in it: the LLM API key (it lives in the OS keychain and never leaves it; the
opt-in plaintext key file ``secrets/api_keys.toml`` is not on the allow-list either, and the
``[secrets]`` opt-in table is dropped from ``config.toml``: whether plaintext keys are allowed is a
decision about one machine, so a restore on another machine can never switch it on), the
extract cache and logs (rebuildable / per-machine), `tax_staging/` (never backed up by design), the
``jobs`` table of the snapshot (in-process queue state that means nothing on another machine) and
the ``extra_headers`` / URL credentials of ``[llm]`` in ``config.toml`` (they can carry secrets).
Only an explicit allow-list of members is ever read from the data directory, so nothing else a
future version leaves there can slip in.

Restore validates everything (decryption, manifest, member names, sizes, hashes, schema revision,
database integrity) in a staging directory that is a sibling of the data directory, migrates it if
the archive is older, and only then swaps it in with two renames. An existing data directory is
moved aside to ``<name>.<timestamp>.before-restore``, never deleted.

The documents folder is different: it may be a mount point (a NAS share) and holds files the user
manages, so it is never swapped or emptied. Its members are extracted into a hidden staging folder
*inside* it (the same filesystem, so a move is a rename), named collision-free against what is
already there (recorded in the staged database before it goes live), and moved into place only
after the data directory swap succeeded. Files already in the folder that are not part of the
backup stay where they are and are counted in the result so the caller can say so. An archive made
by an older version (``statements/<sha256>.pdf``) is converted to the readable layout on the way
in.

The machine-local secret state of the directory being replaced is carried over into the restored
one: its ``secrets/`` directory (the plaintext key file the user chose to keep here) and its
``[secrets]`` table of ``config.toml``. It is moved, not left behind: the file is copied into the
restored directory before the swap (so a restore that fails loses nothing) and, once the swap has
succeeded and the copy is verified, the original in the ``*.before-restore`` directory is
overwritten and removed, so no plaintext key lingers there unnoticed. A restore also never changes
whether the file store is enabled on this machine (a restored archive's own ``[secrets]``, if an
older version wrote one, is discarded).

The archive is treated as hostile input. Extraction counts the bytes it actually writes against
per-member and total limits (never trusting the sizes a crafted zip or manifest declares), and
refuses an archive with an absurd number of members or one that cannot fit on the disk.

Everything plaintext that exists only while a backup is built or restored (the zip before it is
encrypted, a decrypted archive, an upload on its way to a restore, an export on its way to the
download) lives in a private 0700 scratch directory that shares the data's filesystem and
protection, never in the system temp directory (often RAM-backed in a container, and readable by
other local users and their backup tools). It is removed on every exit path, and a directory a
crashed process left behind is swept away once it is old enough (`sweep_stale_work_dirs`).
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import IO, Any
from urllib.parse import quote, urlsplit, urlunsplit

import tomli_w
from alembic.script import ScriptDirectory
from alembic.util import CommandError

from personal_finance_analyzer.backup import crypto
from personal_finance_analyzer.backup.errors import (
    BackupError,
    BackupFormatError,
    PassphraseError,
    RestoreTargetError,
    SchemaTooNewError,
)
from personal_finance_analyzer.core import doc_names
from personal_finance_analyzer.core import documents as docs
from personal_finance_analyzer.core.paths import (
    create_home_layout,
    documents_dir,
    existing_db_file,
    get_home,
)
from personal_finance_analyzer.core.secure_delete import shred_file
from personal_finance_analyzer.db.migrate import build_alembic_config, upgrade_head

FORMAT_NAME = "personal-finance-analyzer-backup"
#: The manifest `format` of archives written before the product was renamed; still accepted.
LEGACY_FORMAT_NAME = "spend-analyzer-backup"
FORMAT_VERSION = 1
#: The extension written. Restore and inspect take any file name, so a `.spendbackup` made before
#: the rename still works.
FILE_EXTENSION = ".pfabackup"

MANIFEST_NAME = "manifest.json"
DB_MEMBER = "db/finance.db"
#: Where archives written before the rename keep the database; still accepted (and never written).
LEGACY_DB_MEMBER = "db/spend.db"
_DB_MEMBERS = (DB_MEMBER, LEGACY_DB_MEMBER)
CONFIG_MEMBER = "config.toml"

#: Tables counted into the manifest (names are constants, never caller input).
_COUNTED_TABLES = ("users", "accounts", "statements", "transactions", "tax_documents")

_COPY_CHUNK = 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024
_PDF_NAME = re.compile(r"^[0-9a-f]{64}\.pdf$")
#: A path component of a ``documents/`` member: portable on Windows, macOS, Linux and SMB.
_DOC_BAD_CHARS = re.compile(r'[\\:<>"|?*\x00-\x1f\x7f]')
_MAX_DOC_DEPTH = 8
DOCUMENTS_PREFIX = "documents/"
_SPEC_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")
_MAX_SPEC_DEPTH = 4

#: Limits on what extraction accepts, so a crafted archive cannot fill the disk. They are well
#: above any real data directory: a statement PDF is capped at 25 MB on upload, config and specs
#: are small text, and the database is the only large member.
MAX_MEMBERS = 50_000
MAX_DB_BYTES = 8 * 1024**3
MAX_PDF_BYTES = 64 * 1024**2
MAX_TEXT_BYTES = 4 * 1024**2
MAX_TOTAL_BYTES = 16 * 1024**3
#: Free space that must remain after the declared sizes are written.
_DISK_MARGIN_BYTES = 64 * 1024**2


@dataclass(frozen=True)
class BackupFile:
    """One archive member (other than the manifest) as listed in the manifest."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    format_version: int
    app_version: str
    schema_revision: str
    created_at: str
    source_platform: str
    counts: dict[str, int]
    files: tuple[BackupFile, ...]
    #: Documents-folder PDFs a *new* backup left out because their name is not portable (never
    #: written to the manifest; reported by the command that made the backup).
    skipped: tuple[str, ...] = ()

    def to_json(self) -> bytes:
        document: dict[str, Any] = {
            "format": FORMAT_NAME,
            "format_version": self.format_version,
            "app_version": self.app_version,
            "schema_revision": self.schema_revision,
            "created_at": self.created_at,
            "source_platform": self.source_platform,
            "counts": self.counts,
            "files": [{"path": f.path, "size": f.size, "sha256": f.sha256} for f in self.files],
        }
        return json.dumps(document, indent=2).encode("utf-8")


@dataclass(frozen=True)
class BackupInfo:
    manifest: Manifest
    encrypted: bool


@dataclass(frozen=True)
class RestoreResult:
    manifest: Manifest
    #: True when the archive's schema was older and migrations were applied during the restore.
    migrated: bool
    #: Where the data directory that was in the way was moved to, if there was one.
    previous_dir: Path | None
    #: The replaced directory's plaintext key file was carried over into the restored one.
    kept_secrets: bool = False
    #: The documents folder the PDFs were restored into (``None``: the archive had none).
    documents_dir: Path | None = None
    #: PDFs newly written into the documents folder.
    documents_restored: int = 0
    #: PDFs of the backup that were already in the folder with the same content (not rewritten).
    documents_already_present: int = 0
    #: Visible files in the folder that are not part of the restored data: left where they are.
    documents_unrelated: int = 0
    #: Set when some PDFs could not be moved in after the swap: the hidden folder still holding
    #: them (move them in by hand; the database already refers to their final names).
    documents_pending: Path | None = None


def app_version() -> str:
    try:
        return metadata.version("personal-finance-analyzer")
    except metadata.PackageNotFoundError:  # pragma: no cover - running from an unbuilt tree
        return "unknown"


def _documents_for(target: Path, explicit_home: Path | None, documents: Path | None) -> Path:
    """The documents folder to back up or restore into: the explicit one, else the configured one
    when working on the configured data directory, else ``<home>/documents``."""
    if documents is not None:
        return documents
    return documents_dir() if explicit_home is None else target / "documents"


def default_filename(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    return f"pfa-{stamp}{FILE_EXTENSION}"


# --------------------------------------------------------------------------------------------
# Schema revisions
# --------------------------------------------------------------------------------------------


def head_revision() -> str:
    head = ScriptDirectory.from_config(build_alembic_config()).get_current_head()
    if head is None:  # pragma: no cover - the package always ships migrations
        raise BackupError("this installation has no database migrations")
    return head


def _is_known_revision(revision: str) -> bool:
    try:
        return (
            ScriptDirectory.from_config(build_alembic_config()).get_revision(revision) is not None
        )
    except CommandError:
        return False


def _db_revision(db_file: Path) -> str | None:
    con = sqlite3.connect(db_file)
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return str(row[0]) if row else None


# --------------------------------------------------------------------------------------------
# Member names and the manifest
# --------------------------------------------------------------------------------------------


def _is_document_member(name: str) -> bool:
    """Whether ``name`` is a ``documents/...pdf`` member with only portable path components."""
    if not name.startswith(DOCUMENTS_PREFIX):
        return False
    parts = PurePosixPath(name).parts
    if PurePosixPath(name).as_posix() != name:  # "a//b", "./a", a trailing slash: not canonical
        return False
    if not 2 <= len(parts) <= _MAX_DOC_DEPTH + 1 or not parts[-1].casefold().endswith(".pdf"):
        return False
    for part in parts[1:]:
        if (
            part in (".", "..")
            or part.startswith(".")
            or part != part.strip()
            or part.endswith(".")
            or _DOC_BAD_CHARS.search(part)
            or len(part.encode("utf-8")) > 255
        ):
            return False
    return True


def _is_allowed_member(name: str) -> bool:
    """Whether ``name`` is a member this format may contain: an allow-list, so a crafted archive
    cannot write outside the staging directory (zip-slip) or add files we never created."""
    if name in (*_DB_MEMBERS, CONFIG_MEMBER):
        return True
    if "\\" in name or name.startswith("/"):
        return False
    if name.startswith(DOCUMENTS_PREFIX):  # readable names: checked per component
        return _is_document_member(name)
    if ":" in name:
        return False
    parts = PurePosixPath(name).parts
    if len(parts) == 2 and parts[0] in ("statements", "incoming"):
        return _PDF_NAME.fullmatch(parts[1]) is not None
    if parts and parts[0] == "specs" and 2 <= len(parts) <= _MAX_SPEC_DEPTH + 1:
        return all(_SPEC_SEGMENT.fullmatch(segment) for segment in parts[1:])
    return False


def _parse_manifest(raw: bytes) -> Manifest:
    try:
        doc = json.loads(raw)
    except ValueError:
        raise BackupFormatError("the backup's manifest is not valid JSON") from None
    if not isinstance(doc, dict) or doc.get("format") not in (
        FORMAT_NAME,
        LEGACY_FORMAT_NAME,
    ):
        raise BackupFormatError("not a Personal Finance Analyzer backup")
    version = doc.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise BackupFormatError("the backup's manifest has no valid format version")
    if version > FORMAT_VERSION:
        raise BackupFormatError(
            f"this backup uses format version {version}, newer than this version of "
            f"Personal Finance Analyzer understands ({FORMAT_VERSION}); upgrade the app and "
            "try again"
        )
    try:
        files = tuple(
            BackupFile(path=str(f["path"]), size=int(f["size"]), sha256=str(f["sha256"]))
            for f in doc["files"]
        )
        counts = {str(k): int(v) for k, v in doc["counts"].items()}
        manifest = Manifest(
            format_version=version,
            app_version=str(doc["app_version"]),
            schema_revision=str(doc["schema_revision"]),
            created_at=str(doc["created_at"]),
            source_platform=str(doc.get("source_platform", "")),
            counts=counts,
            files=files,
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise BackupFormatError("the backup's manifest is incomplete or malformed") from None
    seen: set[str] = set()
    for entry in manifest.files:
        if not _is_allowed_member(entry.path):
            raise BackupFormatError(f"the backup lists an unexpected file: {entry.path!r}")
        if entry.path in seen:
            raise BackupFormatError(f"the backup lists {entry.path!r} twice")
        if entry.size < 0 or re.fullmatch(r"[0-9a-f]{64}", entry.sha256) is None:
            raise BackupFormatError(f"the backup's manifest entry for {entry.path!r} is malformed")
        seen.add(entry.path)
    databases = [member for member in _DB_MEMBERS if member in seen]
    if not databases:
        raise BackupFormatError("the backup contains no database")
    if len(databases) > 1:
        raise BackupFormatError("the backup contains more than one database")
    return manifest


def database_member(manifest: Manifest) -> str:
    """The archive member holding the database: `DB_MEMBER`, or `LEGACY_DB_MEMBER` for a backup
    made before the product was renamed. `_parse_manifest` guarantees exactly one is listed."""
    listed = {f.path for f in manifest.files}
    return LEGACY_DB_MEMBER if LEGACY_DB_MEMBER in listed else DB_MEMBER


# --------------------------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------------------------


def _copy_hashed(
    src: IO[bytes], dst: IO[bytes], *, limit: int | None = None, label: str = ""
) -> tuple[int, str]:
    """Copy ``src`` to ``dst`` and return the byte count and SHA-256. With ``limit`` it stops at the
    first chunk that takes the count past it, before writing that chunk."""
    digest = hashlib.sha256()
    total = 0
    while chunk := src.read(_COPY_CHUNK):
        total += len(chunk)
        if limit is not None and total > limit:
            raise BackupFormatError(f"{label} is larger than the backup's manifest says")
        digest.update(chunk)
        dst.write(chunk)
    return total, digest.hexdigest()


def _strip_url_credentials(url: str) -> str:
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


def _portable_config(raw: bytes) -> bytes:
    """``config.toml`` without anything that can carry a secret: ``[llm] extra_headers`` (e.g. an
    ``Authorization`` header), credentials embedded in ``[llm] base_url``, and the whole
    ``[secrets]`` table (the plaintext-key-file opt-in belongs to this machine and must not travel
    to another one). The API key itself is never in this file (it is in the OS keychain)."""
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise BackupError(f"config.toml is not valid TOML ({exc}); fix or remove it first") from exc
    data.pop("secrets", None)
    llm = data.get("llm")
    if isinstance(llm, dict):
        llm.pop("extra_headers", None)
        base_url = llm.get("base_url")
        if isinstance(base_url, str):
            llm["base_url"] = _strip_url_credentials(base_url)
    return tomli_w.dumps(data).encode("utf-8")


def _snapshot_database(source: Path, dest: Path) -> dict[str, int]:
    """Copy ``source`` to ``dest`` with SQLite's online backup API (a consistent point-in-time
    copy even while another connection writes), drop the machine-local ``jobs`` rows from the
    copy, leave it as a single journal-less file, and return its row counts."""
    src = sqlite3.connect(source)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        dst.execute("DELETE FROM jobs")
        dst.commit()
        dst.execute("PRAGMA journal_mode=DELETE")
        present = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts: dict[str, int] = {}
        for table in _COUNTED_TABLES:
            if table in present:
                counts[table] = int(dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return counts
    except sqlite3.Error as exc:
        raise BackupError(f"could not read the database: {exc}") from exc
    finally:
        dst.close()
        src.close()


def _iter_home_files(home: Path) -> Iterator[tuple[str, Path]]:
    """The allow-listed files of the data directory as ``(member name, path)``: PDFs named by
    their content hash (not the transient upload temp files next to them), in the old
    ``statements/`` folder and in ``incoming/``, and layout specs."""
    for folder in ("statements", "incoming"):
        base = home / folder
        if base.is_dir():
            for path in sorted(base.iterdir()):
                if _PDF_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink():
                    yield f"{folder}/{path.name}", path
    specs = home / "specs"
    if specs.is_dir():
        for path in sorted(specs.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            name = f"specs/{path.relative_to(specs).as_posix()}"
            if _is_allowed_member(name):
                yield name, path


def _iter_document_files(root: Path, skipped: list[str]) -> Iterator[tuple[str, Path]]:
    """The PDFs of the documents folder as ``("documents/<relative path>", path)``. A file whose
    name could not be restored everywhere (a colon, a trailing dot...) is appended to ``skipped``
    instead."""
    for rel, path in docs.iter_files(root):
        name = f"{DOCUMENTS_PREFIX}{rel}"
        if _is_document_member(name):
            yield name, path
        else:
            skipped.append(rel)


def _write_zip(home: Path, zip_path: Path, work: Path, documents: Path) -> Manifest:
    db_file = existing_db_file(home)
    if not db_file.is_file():
        raise BackupError(f"nothing to back up: no database in {home}")
    snapshot = work / "finance.db"
    counts = _snapshot_database(db_file, snapshot)
    revision = _db_revision(snapshot)
    if revision is None:
        raise BackupError("the database has not been migrated; run `pfa migrate` first")

    sources: list[tuple[str, Path]] = [(DB_MEMBER, snapshot)]
    config_file = home / "config.toml"
    if config_file.is_file():
        portable = work / "config.toml"
        portable.write_bytes(_portable_config(config_file.read_bytes()))
        sources.append((CONFIG_MEMBER, portable))
    sources.extend(_iter_home_files(home))
    skipped: list[str] = []
    sources.extend(_iter_document_files(documents, skipped))

    now = datetime.now(UTC)
    entries: list[BackupFile] = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, path in sources:
            info = zipfile.ZipInfo(name, date_time=now.timetuple()[:6])
            info.compress_type = (
                zipfile.ZIP_STORED if name.endswith(".pdf") else zipfile.ZIP_DEFLATED
            )
            try:
                with path.open("rb") as src, zf.open(info, "w", force_zip64=True) as out:
                    size, sha256 = _copy_hashed(src, out)
            except FileNotFoundError:  # removed between listing and reading (e.g. a delete)
                continue
            entries.append(BackupFile(name, size, sha256))
        manifest = Manifest(
            format_version=FORMAT_VERSION,
            app_version=app_version(),
            schema_revision=revision,
            created_at=now.isoformat(timespec="seconds"),
            source_platform=sys.platform,
            counts=counts,
            files=tuple(entries),
            skipped=tuple(skipped),
        )
        zf.writestr(MANIFEST_NAME, manifest.to_json())
    return manifest


#: The private scratch directory inside the data directory (see `make_work_dir`).
WORK_DIRNAME = ".tmp"

#: Work directories older than this are assumed to belong to a process that died (a restore or
#: export takes seconds to minutes) and are removed by `sweep_stale_work_dirs`.
STALE_WORK_DIR_AGE = timedelta(hours=12)

#: Prefixes of the work directories created beside the data directory (see
#: `make_sibling_work_dir`, and `restore_backup`'s staging directory); the sweep only touches
#: names that match exactly, so it can never remove something of the user's.
_SIBLING_KINDS = ("restore-upload", "restore", "backup", "export", "inspect")


def _sibling_pattern(home: Path) -> re.Pattern[str]:
    kinds = "|".join(re.escape(kind) for kind in _SIBLING_KINDS)
    # `tempfile.mkdtemp` appends 8 characters from [a-z0-9_].
    return re.compile(rf"^\.{re.escape(home.name)}\.(?:{kinds})-[a-z0-9_]{{8}}$")


def _newest_mtime(path: Path) -> float:
    """Latest modification time of ``path`` and its direct children: a directory's own mtime does
    not change while a file inside it is being written."""
    newest = path.lstat().st_mtime
    if path.is_dir() and not path.is_symlink():
        for child in path.iterdir():
            with contextlib.suppress(OSError):
                newest = max(newest, child.lstat().st_mtime)
    return newest


def sweep_stale_work_dirs(
    home: Path, *, max_age: timedelta = STALE_WORK_DIR_AGE, now: float | None = None
) -> list[Path]:
    """Remove work directories a crashed backup, export or restore left behind: everything in
    ``<home>/.tmp/`` and the sibling directories of ``home`` named like `make_sibling_work_dir`'s
    and `restore_backup`'s staging directory, whose newest file is older than ``max_age``.
    Best effort (an entry that cannot be removed is skipped); returns what was removed.

    Run at server start and before every backup operation, so a restore that was killed halfway
    does not leave a plaintext copy of the data lying around indefinitely."""
    cutoff = (time.time() if now is None else now) - max_age.total_seconds()
    base = home / WORK_DIRNAME
    candidates: list[Path] = []
    with contextlib.suppress(OSError):
        if base.is_dir() and not base.is_symlink():
            candidates.extend(base.iterdir())
    pattern = _sibling_pattern(home)
    with contextlib.suppress(OSError):
        if home.parent.is_dir():
            candidates.extend(
                child
                for child in home.parent.iterdir()
                if pattern.match(child.name) and child.is_dir() and not child.is_symlink()
            )
    removed: list[Path] = []
    for candidate in candidates:
        try:
            if _newest_mtime(candidate) > cutoff:
                continue
            if candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
        except OSError:
            continue
        removed.append(candidate)
    with contextlib.suppress(OSError):  # still in use, or already gone
        base.rmdir()
    return removed


def make_sibling_work_dir(home: Path, prefix: str) -> Path:
    """A fresh 0700 scratch directory next to ``home`` (same filesystem and parent, so the same
    protection as the data directory). For plaintext that has to survive a swap of ``home``
    itself, such as a restore's upload: a directory inside ``home`` would be moved aside with it.
    Release it with `remove_work_dir`."""
    sweep_stale_work_dirs(home)
    home.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{home.name}.{prefix}-", dir=home.parent))


def make_work_dir(home: Path, prefix: str) -> Path:
    """A fresh 0700 scratch directory for plaintext intermediates (the zip before it is encrypted,
    the decrypted archive of a restore, an export on its way to the download).

    It lives under ``<home>/.tmp/`` (itself 0700), so it shares the data's filesystem and
    protection rather than the system temp directory. When ``home`` does not exist yet (inspecting
    a backup on a machine that has not run the app) it is a sibling of ``home`` instead, and
    nothing is created inside ``home``. Release it with `remove_work_dir`."""
    if not home.is_dir():
        return make_sibling_work_dir(home, prefix)
    sweep_stale_work_dirs(home)
    base = home / WORK_DIRNAME
    base.mkdir(exist_ok=True)
    with contextlib.suppress(OSError):  # no POSIX permission bits on some filesystems
        base.chmod(0o700)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=base))


def remove_work_dir(work: Path) -> None:
    """Delete a work directory, and the shared ``.tmp`` parent once it is empty."""
    shutil.rmtree(work, ignore_errors=True)
    if work.parent.name == WORK_DIRNAME:
        with contextlib.suppress(OSError):  # not empty: another export or restore is running
            work.parent.rmdir()


@contextlib.contextmanager
def work_dir(home: Path, prefix: str) -> Iterator[Path]:
    """`make_work_dir`, removed on exit however the block ends."""
    work = make_work_dir(home, prefix)
    try:
        yield work
    finally:
        remove_work_dir(work)


def create_backup(
    dest: Path,
    *,
    passphrase: str | None,
    home: Path | None = None,
    documents: Path | None = None,
) -> Manifest:
    """Write a backup of ``home`` (default: the data directory) and of the documents folder
    ``documents`` (default: the configured one, or ``<home>/documents`` for an explicit ``home``)
    to ``dest``.

    ``passphrase`` encrypts the archive; ``None`` writes it unencrypted (the caller must have
    asked for that explicitly). The archive is built in a private scratch directory under the
    data directory (never the system temp directory) and moved into place, so ``dest`` never
    holds a half-written file and the plaintext zip never leaves the data's own protection.

    Raises:
        BackupError: no database, an unusable config.toml, or a passphrase that is too short.
    """
    source = home if home is not None else get_home()
    docs_root = _documents_for(source, home, documents)
    if passphrase is not None:
        crypto.validate_new_passphrase(passphrase)
    if not source.is_dir():
        raise BackupError(f"there is no data directory at {source} to back up")
    with work_dir(source, "backup") as work:
        zip_path = work / "archive.zip"
        manifest = _write_zip(source, zip_path, work, docs_root)
        partial = dest.with_name(dest.name + ".partial")
        try:
            if passphrase is None:
                shutil.copyfile(zip_path, partial)
            else:
                with zip_path.open("rb") as plain, partial.open("wb") as sealed:
                    crypto.encrypt_stream(plain, sealed, passphrase, nonce_seed=os.urandom(23))
            with contextlib.suppress(OSError):  # no POSIX permission bits on some filesystems
                partial.chmod(0o600)
            partial.replace(dest)
        finally:
            partial.unlink(missing_ok=True)
    return manifest


# --------------------------------------------------------------------------------------------
# Open / inspect
# --------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _open_archive(
    path: Path, passphrase: str | None, work: Path
) -> Iterator[tuple[zipfile.ZipFile, Manifest, bool]]:
    """Open ``path`` (decrypting it into ``work`` first when needed) and yield the zip, its parsed
    manifest and whether it was encrypted. The zip's member list is checked against the manifest
    here, so every caller sees a structurally valid archive."""
    try:
        with path.open("rb") as fh:
            encrypted = crypto.is_encrypted(fh)
            if encrypted:
                if not passphrase:
                    raise PassphraseError("this backup is encrypted; a passphrase is required")
                plain = work / "archive.zip"
                with plain.open("wb") as out:
                    crypto.decrypt_stream(fh, out, passphrase)
                zip_source = plain
            else:
                zip_source = path
    except OSError as exc:
        raise BackupError(f"cannot read {path}: {exc}") from exc
    try:
        with zipfile.ZipFile(zip_source) as zf:
            names = zf.namelist()
            if len(names) > MAX_MEMBERS:
                raise BackupFormatError(
                    f"the backup holds {len(names)} files, more than a backup may contain "
                    f"({MAX_MEMBERS})"
                )
            if len(set(names)) != len(names):
                raise BackupFormatError("the backup contains duplicate entries")
            if MANIFEST_NAME not in names:
                raise BackupFormatError("not a Personal Finance Analyzer backup (no manifest)")
            if zf.getinfo(MANIFEST_NAME).file_size > _MAX_MANIFEST_BYTES:
                raise BackupFormatError("the backup's manifest is too large")
            manifest = _parse_manifest(zf.read(MANIFEST_NAME))
            listed = {f.path for f in manifest.files} | {MANIFEST_NAME}
            if set(names) != listed:
                raise BackupFormatError("the backup's contents do not match its manifest")
            yield zf, manifest, encrypted
    except zipfile.BadZipFile:
        raise BackupFormatError(
            "not a Personal Finance Analyzer backup (damaged or not a backup file)"
        ) from None


def inspect_backup(path: Path, *, passphrase: str | None = None) -> BackupInfo:
    """Read ``path``'s manifest (counts and file list only; no data is extracted). An encrypted
    backup needs its passphrase, and is authenticated end to end while it is decrypted."""
    with (
        work_dir(get_home(), "inspect") as work,
        _open_archive(path, passphrase, work) as (_zf, manifest, encrypted),
    ):
        return BackupInfo(manifest=manifest, encrypted=encrypted)


# --------------------------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------------------------


def _home_has_data(home: Path) -> bool:
    """Whether ``home`` holds anything a restore would replace: a database, a config, a PDF
    awaiting confirmation or still in the old ``statements/`` folder, or a layout spec. Empty
    directories and logs do not count. (The documents folder is never replaced, so it does not
    count either.)"""
    db_file = existing_db_file(home)
    if (db_file.exists() and not _db_is_pristine(db_file)) or (home / "config.toml").exists():
        return True
    return any(
        child.is_file()
        for sub in ("statements", "incoming", "specs")
        for child in (home / sub).rglob("*")
    )


#: Tables whose rows are the user's own data. The rest (taxonomy, built-in rules, the job
#: queue, the default user) is what a freshly created database already holds.
_USER_DATA_TABLES = (
    "issuers",
    "accounts",
    "layout_specs",
    "statements",
    "transactions",
    "merchant_map",
    "llm_runs",
    "classifications",
    "tax_documents",
    "tax_forms",
    "income_items",
    "tax_items",
    "balance_entries",
    "net_worth_history",
    "forecast_presets",
    "tax_profiles",
)


def _db_is_pristine(db_file: Path) -> bool:
    """Whether ``db_file`` holds no user data: a database a diagnosis or a bare ``pfa migrate``
    created (schema and seeds, at most the default user), so a restore into such a machine does
    not demand ``--replace``. Opened read-only and ``immutable`` (no locks, and no ``-shm``/``-wal``
    files created next to the user's database); a database with a write-ahead log, which an
    immutable open would not see, and anything unreadable count as having data."""
    if db_file.with_name(db_file.name + "-wal").exists():
        return False
    try:
        con = sqlite3.connect(
            f"file:{quote(db_file.as_posix())}?mode=ro&immutable=1", uri=True, timeout=1
        )
        try:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master")}
            if "users" in tables and con.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 1:
                return False
            return not any(
                con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
                for table in _USER_DATA_TABLES
                if table in tables
            )
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _contains_files(home: Path) -> bool:
    return any(p.is_file() or p.is_symlink() for p in home.rglob("*"))


def _db_in_use(db_file: Path) -> bool:
    """Whether another process still has ``db_file`` open, by two independent signs.

    Locks: a connection of ours takes an exclusive lock for an instant. Another process in the
    middle of a transaction (any journal mode, including the rollback journal, which leaves no
    sidecar files) makes that wait out the timeout and fail.

    Sidecar files: in WAL mode (what the app uses) the last connection to close removes the
    ``-wal``/``-shm`` files, so if they survive our own open+close someone else holds the
    database, even one that is sitting idle (a running ``pfa serve``)."""
    if not db_file.exists():
        return False
    try:
        con = sqlite3.connect(db_file, timeout=1, isolation_level=None)
        try:
            con.execute("PRAGMA journal_mode").fetchone()
            con.execute("BEGIN EXCLUSIVE")
            con.execute("ROLLBACK")
        finally:
            con.close()
    except sqlite3.Error:
        return True
    return any(db_file.with_name(db_file.name + suffix).exists() for suffix in ("-wal", "-shm"))


def _check_target(target: Path, *, replace: bool) -> None:
    if target.exists() and _home_has_data(target) and not replace:
        raise RestoreTargetError(
            f"{target} already holds Personal Finance Analyzer data. Restoring replaces it; pass --replace "
            "to go ahead (your current data directory is moved aside, not deleted)."
        )


def _member_limit(path: str) -> int:
    if path == DB_MEMBER:
        return MAX_DB_BYTES
    if path.endswith(".pdf"):
        return MAX_PDF_BYTES
    return MAX_TEXT_BYTES


def _extract(
    zf: zipfile.ZipFile, manifest: Manifest, new_home: Path, doc_stage: Path | None = None
) -> None:
    """Write every member into ``new_home`` (the ``documents/`` ones into ``doc_stage``, under
    their relative path), checking its size and SHA-256 against the manifest while streaming (a
    member that grows past its declared size is aborted, not buffered).

    The manifest is attacker-controlled too, so the sizes it declares are first held to the
    per-member and total limits and to the free disk space (of the filesystem each member goes
    to), and then the bytes actually written are counted against the same limits: a member that
    inflates past what it declared is stopped at that point, never extracted whole."""
    declared_home = 0
    declared_docs = 0
    for entry in manifest.files:
        if entry.size > _member_limit(entry.path):
            raise BackupFormatError(
                f"{entry.path} is larger than a file in a backup may be "
                f"({_member_limit(entry.path)} bytes)"
            )
        if entry.path.startswith(DOCUMENTS_PREFIX):
            declared_docs += entry.size
        else:
            declared_home += entry.size
    if declared_home + declared_docs > MAX_TOTAL_BYTES:
        raise BackupFormatError(
            f"the backup's files add up to more than a backup may hold ({MAX_TOTAL_BYTES} bytes)"
        )
    for needed, where in ((declared_home, new_home.parent), (declared_docs, doc_stage)):
        if where is None or needed == 0:
            continue
        free = shutil.disk_usage(where).free
        if needed + _DISK_MARGIN_BYTES > free:
            raise BackupError(
                f"not enough free disk space to restore this backup: it needs about {needed} "
                f"bytes in {where} and {free} are free"
            )
    written = 0
    for entry in manifest.files:
        if entry.path.startswith(DOCUMENTS_PREFIX):
            assert doc_stage is not None
            dest = doc_stage.joinpath(*PurePosixPath(entry.path).parts[1:])
            docs.make_dirs(dest.parent, within=doc_stage)
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, docs.file_mode())
            sink: IO[bytes] = os.fdopen(fd, "wb")
        else:
            dest = new_home.joinpath(*PurePosixPath(entry.path).parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            sink = dest.open("wb")
        with zf.open(entry.path) as src, sink as out:
            size, sha256 = _copy_hashed(
                src, out, limit=min(entry.size, MAX_TOTAL_BYTES - written), label=entry.path
            )
        written += size
        if size != entry.size or sha256 != entry.sha256:
            raise BackupFormatError(
                f"{entry.path} does not match the backup's manifest (hash mismatch): the file is "
                "corrupted or was modified"
            )


@dataclass(frozen=True)
class _DocumentMove:
    """One staged document and the relative path (inside the documents folder) it ends up at."""

    staged: Path
    rel: str


def _plan_document_moves(
    doc_stage: Path, docs_root: Path, staged_db: Path
) -> tuple[list[_DocumentMove], int]:
    """Decide where every staged document goes, before the data directory is swapped.

    A name free in ``docs_root`` is kept. A name taken by a file with the *same content* is reused
    (counted, nothing rewritten). A name taken by a file with other content gets `` (2)``,
    `` (3)``..., and the staged database is updated to the final name (``stored_path``), so the
    swap makes everything consistent in one step. Returns the moves and the number of PDFs that
    were already present."""
    moves: list[_DocumentMove] = []
    already_present = 0
    remap: dict[str, str] = {}
    taken: set[str] = set()
    for rel, staged in docs.iter_files(doc_stage):
        directory = docs_root.joinpath(*PurePosixPath(rel).parts[:-1])
        try:
            listing = {p.name.casefold(): p.name for p in directory.iterdir()}
        except OSError:
            listing = {}
        folder = PurePosixPath(rel).parent.as_posix()
        folder = "" if folder == "." else folder
        stem = PurePosixPath(rel).name[: -len(doc_names.PDF_SUFFIX)]
        digest = docs.file_sha256(staged)
        for attempt in itertools.count(1):
            name = doc_names.file_name(stem, doc_names.collision_suffix(attempt))
            final = f"{folder}/{name}" if folder else name
            existing = listing.get(name.casefold())
            if existing is not None:
                if docs.file_sha256(directory / existing) == digest:
                    final = f"{folder}/{existing}" if folder else existing
                    already_present += 1
                    break
                continue
            if final.casefold() in taken:
                continue
            taken.add(final.casefold())
            moves.append(_DocumentMove(staged, final))
            break
        if final != rel:
            remap[rel] = final
    if remap:
        con = sqlite3.connect(staged_db)
        try:
            tables = {
                row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            for table in ("statements", "tax_documents"):
                if table not in tables:
                    continue
                for old, new in remap.items():
                    con.execute(
                        f"UPDATE {table} SET stored_path = ? WHERE stored_path = ?", (new, old)
                    )
            con.commit()
        finally:
            con.close()
    return moves, already_present


def _referenced_paths(db_file: Path) -> set[str]:
    """Every relative ``stored_path`` of the database at ``db_file``, casefolded."""
    found: set[str] = set()
    con = sqlite3.connect(db_file)
    try:
        tables = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table in ("statements", "tax_documents"):
            if table in tables:
                found.update(
                    str(row[0]).casefold()
                    for row in con.execute(
                        f"SELECT stored_path FROM {table} WHERE stored_path IS NOT NULL"
                    )
                )
    finally:
        con.close()
    return found


def _carry_over_legacy_documents(staged_db: Path, new_home: Path, doc_stage: Path) -> None:
    """An archive made before documents had their own folder holds ``statements/<sha256>.pdf``:
    file those into ``doc_stage`` under readable names (and the unconfirmed ones into the staged
    ``incoming/``), exactly like the startup upgrade does for a live data directory."""
    from personal_finance_analyzer.db.documents import migrate_legacy_files
    from personal_finance_analyzer.db.session import make_engine_for_path, make_session_factory

    engine = make_engine_for_path(staged_db)
    try:
        with make_session_factory(engine)() as session:
            report = migrate_legacy_files(
                session,
                legacy_dir=new_home / "statements",
                root=doc_stage,
                staging=new_home / "incoming",
            )
            if report.errors:
                raise BackupError(
                    "could not convert the backup's PDFs to the readable layout: "
                    + "; ".join(report.errors)
                )
    finally:
        engine.dispose()


def _verify_database(db_file: Path, expected_revision: str) -> None:
    con = sqlite3.connect(db_file)
    try:
        problems = con.execute("PRAGMA integrity_check").fetchall()
        if problems != [("ok",)]:
            raise BackupFormatError("the backup's database failed SQLite's integrity check")
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise BackupFormatError("the backup's database has broken references")
    except sqlite3.DatabaseError as exc:
        raise BackupFormatError(f"the backup's database is unreadable: {exc}") from exc
    finally:
        con.close()
    if _db_revision(db_file) != expected_revision:
        raise BackupFormatError("the backup's database does not match its manifest")


def _migrate_staged(db_file: Path) -> None:
    from personal_finance_analyzer.api.services import crud
    from personal_finance_analyzer.classify.rules import sync_builtin_rules
    from personal_finance_analyzer.classify.taxonomy import sync_taxonomy
    from personal_finance_analyzer.db.session import make_engine_for_path, make_session_factory

    upgrade_head(f"sqlite:///{db_file}")
    # What `pfa migrate` does after upgrading: bring seeded taxonomy and rules up to
    # date, which an older database lacks.
    engine = make_engine_for_path(db_file)
    try:
        with make_session_factory(engine)() as session:
            sync_taxonomy(session)
            sync_builtin_rules(session)
            crud.ensure_default_user(session)
            session.commit()
    finally:
        engine.dispose()


def _carry_over_secrets(old_home: Path, new_home: Path) -> bool:
    """Bring this machine's own secret state into ``new_home`` (the restored directory, not yet in
    place): the ``secrets/`` directory of ``old_home`` (the plaintext key file the user opted into
    here) and its ``[secrets]`` table of ``config.toml``, overwriting whatever ``[secrets]`` the
    archive carried. Whether this machine allows a plaintext key file is this machine's decision,
    not the archive's. Returns whether a key file was carried over."""
    secrets_table: dict[str, Any] = {}
    try:
        old_config = tomllib.loads((old_home / "config.toml").read_text(encoding="utf-8"))
        candidate = old_config.get("secrets")
        if isinstance(candidate, dict):
            secrets_table = candidate
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        pass  # no (readable) config: nothing to carry
    new_config_path = new_home / CONFIG_MEMBER
    try:
        data = tomllib.loads(new_config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        data = {}
    data.pop("secrets", None)
    if secrets_table:
        data["secrets"] = secrets_table
    if data:
        new_config_path.write_text(tomli_w.dumps(data), encoding="utf-8")
    old_secrets = old_home / "secrets"
    if not old_secrets.is_dir() or not any(old_secrets.iterdir()):
        return False
    shutil.copytree(old_secrets, new_home / "secrets", dirs_exist_ok=True)
    with contextlib.suppress(OSError):
        (new_home / "secrets").chmod(0o700)
    return True


def _discard_old_secrets(previous: Path, target: Path) -> None:
    """After a successful swap, remove the plaintext key file from the ``*.before-restore``
    directory it was copied out of, so the restore *moves* it: the copy in ``target`` (made before
    the swap, so that a failed restore loses nothing) is verified file by file against the old one
    before the old one is overwritten and deleted. A file whose copy is missing or different is
    left alone. Best effort: the restore has already succeeded, so a failure here is not an error."""
    old_secrets = previous / "secrets"
    new_secrets = target / "secrets"
    if not old_secrets.is_dir() or old_secrets.is_symlink():
        return
    try:
        for old in sorted(p for p in old_secrets.rglob("*") if p.is_file()):
            new = new_secrets / old.relative_to(old_secrets)
            if new.is_file() and new.read_bytes() == old.read_bytes():
                shred_file(old)
        for directory in sorted((p for p in old_secrets.rglob("*") if p.is_dir()), reverse=True):
            with contextlib.suppress(OSError):
                directory.rmdir()
        with contextlib.suppress(OSError):
            old_secrets.rmdir()
    except OSError:
        return


def _remove_empty_tree(root: Path) -> None:
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        directory.rmdir()
    root.rmdir()


def _move_aside(target: Path, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%S")
    aside = target.with_name(f"{target.name}.{stamp}.before-restore")
    counter = 1
    while aside.exists():
        counter += 1
        aside = target.with_name(f"{target.name}.{stamp}-{counter}.before-restore")
    target.rename(aside)
    return aside


def restore_backup(
    archive: Path,
    *,
    passphrase: str | None,
    replace: bool = False,
    home: Path | None = None,
    before_swap: Callable[[], None] | None = None,
    documents: Path | None = None,
) -> RestoreResult:
    """Restore ``archive`` as the data directory ``home`` (default: the configured one) and its
    PDFs into the documents folder ``documents`` (default: the configured one, or
    ``<home>/documents`` for an explicit ``home``).

    Nothing in ``home`` is touched until the archive has been fully decrypted, verified and (if
    its schema is older) migrated in a staging directory next to it. ``before_swap`` runs right
    before the swap; a running server uses it to stop serving and close its database connections.
    The documents are written *in place* (the folder may be a mount point): staged inside it,
    moved to their final names only once the swap has succeeded, never overwriting a file, and
    leaving every other file of the folder alone.

    Raises:
        PassphraseError: encrypted and the passphrase is missing or wrong.
        BackupFormatError: not a backup, damaged, or modified.
        SchemaTooNewError: made by a newer version of the app than this one.
        RestoreTargetError: ``home`` has data and ``replace`` is false, or its database is open
            in another process.
    """
    target = home if home is not None else get_home()
    docs_root = _documents_for(target, home, documents)
    _check_target(target, replace=replace)
    sweep_stale_work_dirs(target)  # what a restore that crashed earlier left behind
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    doc_stage: Path | None = None
    keep_doc_stage = False
    try:
        work = stage / "work"
        new_home = stage / "home"
        work.mkdir()
        with _open_archive(archive, passphrase, work) as (zf, manifest, _encrypted):
            revision = manifest.schema_revision
            if not _is_known_revision(revision):
                raise SchemaTooNewError(
                    f"this backup's database (schema {revision}) is newer than this version of "
                    "Personal Finance Analyzer understands; upgrade the app, then restore it again"
                )
            listed = [f.path for f in manifest.files]
            has_documents = any(p.startswith(DOCUMENTS_PREFIX) for p in listed)
            has_legacy = any(p.startswith("statements/") for p in listed)
            if has_documents or has_legacy:
                doc_stage = _make_doc_stage(docs_root)
            try:
                _extract(zf, manifest, new_home, doc_stage)
            except OSError as exc:
                raise BackupError(
                    f"could not write the restored files (is the disk full?): {exc}"
                ) from exc
        shutil.rmtree(work)  # the decrypted archive is plaintext; do not leave it lying around
        create_home_layout(new_home)
        staged_db = new_home / "finance.db"
        (new_home / database_member(manifest)).replace(staged_db)
        (new_home / "db").rmdir()
        _verify_database(staged_db, revision)
        migrated = revision != head_revision()
        if migrated:
            _migrate_staged(staged_db)
        moves: list[_DocumentMove] = []
        already_present = 0
        if doc_stage is not None:
            if has_legacy:
                _carry_over_legacy_documents(staged_db, new_home, doc_stage)
                shutil.rmtree(new_home / "statements", ignore_errors=True)
            moves, already_present = _plan_document_moves(doc_stage, docs_root, staged_db)

        if before_swap is not None:
            before_swap()
        _check_target(target, replace=replace)
        kept_secrets = _carry_over_secrets(target, new_home) if target.is_dir() else False
        if _db_in_use(existing_db_file(target)):
            raise RestoreTargetError(
                "the current database is open in another process (is `pfa serve` "
                "running?). Stop it, then restore again."
            )
        previous: Path | None = None
        now = datetime.now(UTC)
        carried = _carry_documents_folder(target, docs_root, new_home)
        try:
            if target.exists():
                if _contains_files(target):
                    previous = _move_aside(target, now)
                else:
                    _remove_empty_tree(target)
            new_home.rename(target)
        except OSError as exc:
            if previous is not None and not target.exists():
                previous.rename(target)
            _undo_carry(carried)
            raise BackupError(f"could not move the restored data into place: {exc}") from exc
        if kept_secrets and previous is not None:
            _discard_old_secrets(previous, target)

        # The database now names the final files; put them there. A failure here cannot undo the
        # swap, so what could not be moved stays staged (and is reported) instead of being lost.
        restored, failed = _move_documents_in(moves, docs_root)
        keep_doc_stage = bool(failed)
        referenced = _referenced_paths(existing_db_file(target)) | {m.rel.casefold() for m in moves}
        unrelated = sum(
            1
            for rel, _path in docs.iter_files(docs_root, pdf_only=False)
            if rel.casefold() not in referenced
        )
        docs.invalidate_index()
        return RestoreResult(
            manifest=manifest,
            migrated=migrated,
            previous_dir=previous,
            kept_secrets=kept_secrets,
            documents_dir=docs_root if doc_stage is not None else None,
            documents_restored=restored,
            documents_already_present=already_present,
            documents_unrelated=unrelated,
            documents_pending=doc_stage if keep_doc_stage else None,
        )
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if doc_stage is not None and not keep_doc_stage:
            shutil.rmtree(doc_stage, ignore_errors=True)


def _carry_documents_folder(
    target: Path, docs_root: Path, new_home: Path
) -> tuple[Path, Path] | None:
    """When the documents folder is the default one *inside* the data directory, the swap would
    carry the user's PDFs off to ``*.before-restore`` with everything else. The folder is theirs,
    not part of the data being replaced, so it is moved (a rename) into the restored directory
    first, together with the hidden staging folder that holds the backup's PDFs; it ends up at the
    same path. Returns ``(where it was, where it is now)``, or ``None`` when there was nothing to
    carry."""
    if docs_root == target or target not in docs_root.parents or not docs_root.is_dir():
        return None
    destination = new_home / docs_root.relative_to(target)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        docs_root.rename(destination)
    except OSError as exc:
        raise BackupError(f"could not keep your documents folder in place: {exc}") from exc
    return docs_root, destination


def _undo_carry(carried: tuple[Path, Path] | None) -> None:
    if carried is not None:
        carried[0].parent.mkdir(parents=True, exist_ok=True)
        carried[1].rename(carried[0])


def _make_doc_stage(docs_root: Path) -> Path:
    """A fresh hidden staging folder inside the documents folder (same filesystem, so moving a
    file out of it is a rename, and invisible to everything that lists the folder)."""
    try:
        docs.make_dirs(docs_root)
        stage = Path(tempfile.mkdtemp(prefix=".pfa-restore-", dir=docs_root))
    except OSError as exc:
        raise BackupError(f"cannot write to the documents folder {docs_root}: {exc}") from exc
    with contextlib.suppress(OSError):
        stage.chmod(0o700)
    return stage


def _move_documents_in(moves: Sequence[_DocumentMove], docs_root: Path) -> tuple[int, list[str]]:
    """Move each staged PDF to its final name (never replacing a file). Returns how many were
    placed and the names that could not be."""
    restored = 0
    failed: list[str] = []
    for move in moves:
        final = docs_root.joinpath(*PurePosixPath(move.rel).parts)
        try:
            docs.make_dirs(final.parent, within=docs_root)
            docs.publish_no_clobber_rename(move.staged, final)
        except OSError:
            failed.append(move.rel)
            continue
        restored += 1
    return restored, failed
