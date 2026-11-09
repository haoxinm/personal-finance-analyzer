"""Create, inspect and restore a `.pfabackup` archive (export -> transfer -> import).

Format (version 1): a zip file, optionally wrapped in `crypto.py`'s passphrase encryption, holding

- ``manifest.json``: format name/version, app version, alembic schema revision, creation time,
  row counts (no PII) and every other member's path, size and SHA-256;
- ``db/finance.db``: a consistent snapshot taken with SQLite's online backup API, so it is safe to
  take while ``pfa serve`` is running;
- ``statements/<sha256>.pdf``: the kept PDF copies (and PDFs awaiting confirmation);
- ``specs/...``: layout-spec mirrors, and ``config.toml``.

Deliberately **not** in it: the LLM API key (it lives in the OS keychain and never leaves it), the
extract cache and logs (rebuildable / per-machine), `tax_staging/` (never backed up by design), the
``jobs`` table of the snapshot (in-process queue state that means nothing on another machine) and
the ``extra_headers`` / URL credentials of ``[llm]`` in ``config.toml`` (they can carry secrets).
Only an explicit allow-list of members is ever read from the data directory, so nothing else a
future version leaves there can slip in.

Restore validates everything (decryption, manifest, member names, sizes, hashes, schema revision,
database integrity) in a staging directory that is a sibling of the data directory, migrates it if
the archive is older, and only then swaps it in with two renames. An existing data directory is
moved aside to ``<name>.<timestamp>.before-restore``, never deleted.

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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import IO, Any
from urllib.parse import urlsplit, urlunsplit

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
from personal_finance_analyzer.core.paths import create_home_layout, existing_db_file, get_home
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


def app_version() -> str:
    try:
        return metadata.version("personal-finance-analyzer")
    except metadata.PackageNotFoundError:  # pragma: no cover - running from an unbuilt tree
        return "unknown"


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


def _is_allowed_member(name: str) -> bool:
    """Whether ``name`` is a member this format may contain: an allow-list, so a crafted archive
    cannot write outside the staging directory (zip-slip) or add files we never created."""
    if name in (*_DB_MEMBERS, CONFIG_MEMBER):
        return True
    if "\\" in name or name.startswith("/") or ":" in name:
        return False
    parts = PurePosixPath(name).parts
    if len(parts) == 2 and parts[0] == "statements":
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
    ``Authorization`` header) and credentials embedded in ``[llm] base_url``. The API key itself
    is never in this file (it is in the OS keychain)."""
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise BackupError(f"config.toml is not valid TOML ({exc}); fix or remove it first") from exc
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
    their content hash (not the transient upload temp files next to them) and layout specs."""
    statements = home / "statements"
    if statements.is_dir():
        for path in sorted(statements.iterdir()):
            if _PDF_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink():
                yield f"statements/{path.name}", path
    specs = home / "specs"
    if specs.is_dir():
        for path in sorted(specs.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            name = f"specs/{path.relative_to(specs).as_posix()}"
            if _is_allowed_member(name):
                yield name, path


def _write_zip(home: Path, zip_path: Path, work: Path) -> Manifest:
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


def create_backup(dest: Path, *, passphrase: str | None, home: Path | None = None) -> Manifest:
    """Write a backup of ``home`` (default: the data directory) to ``dest``.

    ``passphrase`` encrypts the archive; ``None`` writes it unencrypted (the caller must have
    asked for that explicitly). The archive is built in a private scratch directory under the
    data directory (never the system temp directory) and moved into place, so ``dest`` never
    holds a half-written file and the plaintext zip never leaves the data's own protection.

    Raises:
        BackupError: no database, an unusable config.toml, or a passphrase that is too short.
    """
    source = home if home is not None else get_home()
    if passphrase is not None:
        crypto.validate_new_passphrase(passphrase)
    if not source.is_dir():
        raise BackupError(f"there is no data directory at {source} to back up")
    with work_dir(source, "backup") as work:
        zip_path = work / "archive.zip"
        manifest = _write_zip(source, zip_path, work)
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
    """Whether ``home`` holds anything a restore would replace: a database, a config, a kept
    statement PDF or a layout spec. Empty directories and logs do not count."""
    if existing_db_file(home).exists() or (home / "config.toml").exists():
        return True
    return any(
        child.is_file() for sub in ("statements", "specs") for child in (home / sub).rglob("*")
    )


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


def _extract(zf: zipfile.ZipFile, manifest: Manifest, new_home: Path) -> None:
    """Write every member into ``new_home``, checking its size and SHA-256 against the manifest
    while streaming (a member that grows past its declared size is aborted, not buffered).

    The manifest is attacker-controlled too, so the sizes it declares are first held to the
    per-member and total limits and to the free disk space, and then the bytes actually written
    are counted against the same limits: a member that inflates past what it declared is stopped
    at that point, never extracted whole."""
    declared = 0
    for entry in manifest.files:
        if entry.size > _member_limit(entry.path):
            raise BackupFormatError(
                f"{entry.path} is larger than a file in a backup may be "
                f"({_member_limit(entry.path)} bytes)"
            )
        declared += entry.size
    if declared > MAX_TOTAL_BYTES:
        raise BackupFormatError(
            f"the backup's files add up to more than a backup may hold ({MAX_TOTAL_BYTES} bytes)"
        )
    free = shutil.disk_usage(new_home.parent).free
    if declared + _DISK_MARGIN_BYTES > free:
        raise BackupError(
            f"not enough free disk space to restore this backup: it needs about {declared} bytes "
            f"and {free} are free"
        )
    written = 0
    for entry in manifest.files:
        dest = new_home.joinpath(*PurePosixPath(entry.path).parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(entry.path) as src, dest.open("wb") as out:
            size, sha256 = _copy_hashed(
                src, out, limit=min(entry.size, MAX_TOTAL_BYTES - written), label=entry.path
            )
        written += size
        if size != entry.size or sha256 != entry.sha256:
            raise BackupFormatError(
                f"{entry.path} does not match the backup's manifest (hash mismatch): the file is "
                "corrupted or was modified"
            )


def _final_name(stored_path: str) -> str:
    # Archives made on Windows hold backslash paths, which a POSIX Path would not split.
    return PureWindowsPath(stored_path).name


def _rewrite_stored_paths(db_file: Path, new_home: Path, final_home: Path) -> None:
    """`statements.stored_path` / `tax_documents.stored_path` are absolute paths on the machine
    that made the backup. Point each at the same hash-named PDF in this machine's data directory
    (``final_home``, where ``new_home`` ends up), or clear it when that PDF was not backed up."""
    con = sqlite3.connect(db_file)
    try:
        tables = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table in ("statements", "tax_documents"):
            if table not in tables:
                continue
            rows = con.execute(
                f"SELECT id, stored_path FROM {table} WHERE stored_path IS NOT NULL"
            ).fetchall()
            for row_id, stored in rows:
                name = _final_name(stored)
                kept = (new_home / "statements" / name).is_file()
                new_value = str(final_home / "statements" / name) if kept else None
                con.execute(f"UPDATE {table} SET stored_path = ? WHERE id = ?", (new_value, row_id))
        con.commit()
    finally:
        con.close()


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
) -> RestoreResult:
    """Restore ``archive`` as the data directory ``home`` (default: the configured one).

    Nothing in ``home`` is touched until the archive has been fully decrypted, verified and (if
    its schema is older) migrated in a staging directory next to it. ``before_swap`` runs right
    before the swap; a running server uses it to stop serving and close its database connections.

    Raises:
        PassphraseError: encrypted and the passphrase is missing or wrong.
        BackupFormatError: not a backup, damaged, or modified.
        SchemaTooNewError: made by a newer version of the app than this one.
        RestoreTargetError: ``home`` has data and ``replace`` is false, or its database is open
            in another process.
    """
    target = home if home is not None else get_home()
    _check_target(target, replace=replace)
    sweep_stale_work_dirs(target)  # what a restore that crashed earlier left behind
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
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
            try:
                _extract(zf, manifest, new_home)
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
        _rewrite_stored_paths(staged_db, new_home, target)
        migrated = revision != head_revision()
        if migrated:
            _migrate_staged(staged_db)

        if before_swap is not None:
            before_swap()
        _check_target(target, replace=replace)
        if _db_in_use(existing_db_file(target)):
            raise RestoreTargetError(
                "the current database is open in another process (is `pfa serve` "
                "running?). Stop it, then restore again."
            )
        previous: Path | None = None
        now = datetime.now(UTC)
        if target.exists():
            if _contains_files(target):
                previous = _move_aside(target, now)
            else:
                _remove_empty_tree(target)
        try:
            new_home.rename(target)
        except OSError as exc:
            if previous is not None:
                previous.rename(target)
            raise BackupError(f"could not move the restored data into place: {exc}") from exc
        return RestoreResult(manifest=manifest, migrated=migrated, previous_dir=previous)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
