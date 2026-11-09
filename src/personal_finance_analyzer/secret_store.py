"""Where API keys can live, and why a given machine can or cannot store one (I7).

Two backends, plus an environment override handled in `config.py`:

1. The **OS keychain** through `keyring`: macOS Keychain, Windows Credential Manager, or a Linux
   Secret Service (GNOME Keyring / KWallet over D-Bus). This is the default and the only backend
   that is ever written to without being asked.
2. An **opt-in file store** (``<data dir>/secrets/api_keys.toml``, directory 0700, file 0600,
   written atomically) for machines that have no keychain at all (headless Linux, WSL, SSH
   sessions, containers). It is plaintext at rest, protected only by file permissions, so it is
   used for *saving* a key only when the keychain is unavailable **and** the user switched it on
   (``[secrets] allow_file_store = true``). Nothing ever falls back to it silently.

This module has no knowledge of `Settings`; `config.py` decides which backend to use and calls
down here. It never logs or returns a key except from `read_file_key`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import sys
import tempfile
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import keyring
import keyring.backend
import keyring.errors
import tomli_w

from personal_finance_analyzer.core.errors import PfaError
from personal_finance_analyzer.core.paths import get_home
from personal_finance_analyzer.core.secure_delete import shred_file

_log = logging.getLogger(__name__)

SecretSource = Literal["env", "keychain", "file", "none"]


class SecretStorageError(PfaError):
    """A key could not be stored or removed. The message is user-facing: it names the cause and
    what to do about it, and never contains a key."""


# --- the OS keychain -------------------------------------------------------------------------

#: Human names of the keyring backends we recognise, by ``module.Class``.
_BACKEND_NAMES: tuple[tuple[str, str], ...] = (
    ("keyring.backends.macOS.Keyring", "macOS Keychain"),
    ("keyring.backends.Windows.WinVaultKeyring", "Windows Credential Manager"),
    ("keyring.backends.SecretService.Keyring", "Secret Service (GNOME Keyring / KWallet)"),
    ("keyring.backends.kwallet.DBusKeyring", "KWallet"),
    ("keyring.backends.kwallet.DBusKeyringKWallet4", "KWallet"),
    ("keyring.backends.libsecret.Keyring", "libsecret (Secret Service)"),
)
#: Backends that exist but cannot store anything (``fail`` raises on use, ``null`` silently
#: discards writes, which is worse): never treated as a keychain.
_UNAVAILABLE_BACKENDS = ("keyring.backends.fail.Keyring", "keyring.backends.null.Keyring")


@dataclass(frozen=True)
class KeychainStatus:
    """What `keyring` resolved to on this machine, without reading or writing any secret."""

    usable: bool
    #: Human name ("macOS Keychain"), or a description of why there is none.
    name: str
    #: ``module.Class`` of the active backend (after unwrapping a chainer), for diagnostics.
    backend: str
    #: Why it is not usable ("" when it is).
    detail: str = ""


def _qualified_name(backend: object) -> str:
    cls = type(backend)
    return f"{cls.__module__}.{cls.__name__}"


def _unwrap(backend: keyring.backend.KeyringBackend) -> keyring.backend.KeyringBackend | None:
    """The backend a `ChainerBackend` will actually use first (None: it has none)."""
    from keyring.backends.chainer import ChainerBackend

    if isinstance(backend, ChainerBackend):
        members = list(backend.backends)
        return members[0] if members else None
    return backend


def inspect_keychain() -> KeychainStatus:
    """Resolve the active keyring backend and say whether it can hold a secret. Read-only: loading
    a backend never touches the keychain (no unlock prompt, no dialog)."""
    try:
        active = keyring.get_keyring()
        backend = _unwrap(active)
    except Exception as exc:  # a broken backend plugin must not take the whole app down
        return KeychainStatus(False, "none", "unknown", f"keyring failed to load ({exc})")
    if backend is None:
        return KeychainStatus(False, "none", _qualified_name(active), "no keyring backend found")
    qualified = _qualified_name(backend)
    if qualified in _UNAVAILABLE_BACKENDS:
        reason = (
            "keyring is disabled (null backend)"
            if qualified.endswith("null.Keyring")
            else "no usable keyring backend is installed or running"
        )
        return KeychainStatus(False, "none", qualified, reason)
    if qualified.startswith("keyrings.alt."):
        return KeychainStatus(
            False,
            "none",
            qualified,
            "the active keyring backend stores secrets in plaintext (keyrings.alt); "
            "refusing to use it",
        )
    for known, name in _BACKEND_NAMES:
        if qualified == known:
            return KeychainStatus(True, name, qualified)
    return KeychainStatus(True, f"custom keyring backend ({qualified})", qualified)


def platform_guidance(platform: str | None = None) -> str:
    """Setup advice for getting an OS keychain working on ``platform`` (default: this one)."""
    plat = platform if platform is not None else sys.platform
    if plat == "darwin":
        return (
            "macOS: the login keychain must exist and be unlocked. Open Keychain Access and "
            "check that a 'login' keychain is listed (File > New Keychain, or Keychain Access > "
            "Settings > Reset My Default Keychain), or run "
            "`security unlock-keychain ~/Library/Keychains/login.keychain-db`."
        )
    if plat.startswith("win") or plat == "cygwin":
        return (
            "Windows: the Credential Manager backend needs no setup. If it fails, make sure you "
            "are not running as a service account without a profile, and that the "
            "'Credential Manager' service is running."
        )
    return (
        "Linux: install and start a Secret Service provider and run in a D-Bus session. "
        "Debian/Ubuntu: `sudo apt install gnome-keyring libsecret-1-0` (KDE: `kwalletmanager`); "
        "Fedora: `sudo dnf install gnome-keyring libsecret`; "
        "Arch: `sudo pacman -S gnome-keyring libsecret`. Log in to a desktop session so the "
        "keyring is unlocked. Over SSH, in WSL or in a container there is usually no D-Bus "
        "session: start one with `dbus-run-session -- bash` and unlock the keyring with "
        "`echo -n 'password' | gnome-keyring-daemon --unlock --components=secrets`, or skip the "
        "keychain with the PFA_LLM_API_KEY environment variable, or (plaintext, last resort) "
        "set `[secrets] allow_file_store = true` in config.toml."
    )


#: What to tell a user who turned the file store on (or is about to).
FILE_STORE_WARNING = (
    "File store: API keys are saved as plaintext in {path}, protected only by file permissions "
    "(0600), and are not encrypted or backed up. Anyone who can read your account's files, or "
    "a copy of this directory, can read them. Prefer the OS keychain or the PFA_LLM_API_KEY "
    "environment variable when you can."
)


def explain_keyring_error(exc: BaseException, *, platform: str | None = None) -> str:
    """A clear, actionable sentence for a `keyring` failure. Never includes a secret: keyring's own
    messages carry only the OS error."""
    text = str(exc)
    lowered = text.lower()
    plat = platform if platform is not None else sys.platform
    if isinstance(exc, keyring.errors.NoKeyringError):
        cause = "No system keychain is available on this machine."
    elif isinstance(exc, keyring.errors.KeyringLocked) or "locked" in lowered:
        cause = "The system keychain is locked. Unlock it and try again."
    elif plat == "darwin" and (
        "-25307" in text or "keychain cannot be found" in lowered or "no such keychain" in lowered
    ):
        cause = "macOS reports that the login keychain cannot be found."
    elif isinstance(exc, keyring.errors.InitError):
        cause = f"The system keychain could not be initialised ({text or type(exc).__name__})."
    else:
        cause = f"The system keychain refused the request ({type(exc).__name__}: {text})."
    return f"{cause} {platform_guidance(plat)}"


# --- the opt-in file store -------------------------------------------------------------------

_FILE_LOCK = threading.Lock()
_FILE_NAME = "api_keys.toml"


def file_store_path() -> Path:
    return get_home() / "secrets" / _FILE_NAME


def _chmod(path: Path, mode: int) -> None:
    # Windows only toggles the read-only bit, and some mounts refuse chmod; the per-user profile
    # directory's ACLs are the protection there.
    with contextlib.suppress(OSError):
        path.chmod(mode)


def _load(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    if os.name == "posix" and path.stat().st_mode & 0o077:
        _chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # tighten a file someone loosened
    try:
        with path.open("rb") as f:
            table = tomllib.load(f).get("keys", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SecretStorageError(
            f"The key file {path} could not be read ({type(exc).__name__}); fix or delete it."
        ) from None
    return {str(k): v for k, v in table.items() if isinstance(v, str)}


def _store(path: Path, keys: dict[str, str]) -> None:
    """Atomically replace ``path`` with ``keys`` (0600, in a 0700 directory)."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    _chmod(directory, stat.S_IRWXU)
    fd, tmp_name = tempfile.mkstemp(prefix=".api_keys-", suffix=".tmp", dir=directory)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:  # mkstemp creates the file 0600
            tomli_w.dump({"keys": keys}, f)
            f.flush()
            os.fsync(f.fileno())
        _chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_file_key(account: str) -> str | None:
    with _FILE_LOCK:
        return _load(file_store_path()).get(account)


def file_store_file_exists() -> bool:
    """Whether the key file is on disk at all, readable or not (a corrupt one still holds, or may
    hold, plaintext keys, so it counts when deciding what to do about leftover keys)."""
    return file_store_path().is_file()


def read_all_file_keys() -> dict[str, str]:
    """Every ``account -> key`` pair in the key file (empty when there is none)."""
    with _FILE_LOCK:
        return _load(file_store_path())


def file_store_has_keys() -> bool:
    try:
        with _FILE_LOCK:
            return bool(_load(file_store_path()))
    except SecretStorageError:
        return False


def write_file_key(account: str, key: str) -> None:
    path = file_store_path()
    try:
        with _FILE_LOCK:
            keys = _load(path)
            keys[account] = key
            _store(path, keys)
    except OSError as exc:
        raise SecretStorageError(
            f"The key file {path} could not be written ({type(exc).__name__}: {exc.strerror})."
        ) from None


def delete_file_key(account: str) -> None:
    """Remove ``account`` from the file store if present (always allowed: deleting plaintext is
    never wrong, so this ignores the opt-in)."""
    path = file_store_path()
    try:
        with _FILE_LOCK:
            keys = _load(path)
            if account not in keys:
                return
            del keys[account]
            if keys:
                _store(path, keys)
            else:
                shred_file(path)
                _remove_dir_if_empty(path.parent)
    except OSError as exc:
        raise SecretStorageError(
            f"The key file {path} could not be updated ({type(exc).__name__}: {exc.strerror})."
        ) from None


def _remove_dir_if_empty(directory: Path) -> None:
    with contextlib.suppress(OSError):  # not empty (or already gone): leave it
        directory.rmdir()


def remove_file_store() -> bool:
    """Delete the whole plaintext key file (every account in it), overwriting it first, along with
    any half-written temporary copy next to it. Returns whether anything was removed. Always
    allowed, whatever the opt-in says: removing plaintext is never wrong.

    Raises:
        SecretStorageError: the file could not be removed (permissions, a read-only mount)."""
    path = file_store_path()
    try:
        with _FILE_LOCK:
            existed = path.is_file() or path.is_symlink()
            shred_file(path)
            if path.parent.is_dir():
                for leftover in path.parent.glob(".api_keys-*.tmp"):
                    existed = True
                    shred_file(leftover)
                _remove_dir_if_empty(path.parent)
    except OSError as exc:
        raise SecretStorageError(
            f"The key file {path} could not be deleted ({type(exc).__name__}: {exc.strerror})."
        ) from None
    return existed
