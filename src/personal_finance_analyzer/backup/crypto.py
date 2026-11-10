"""Passphrase encryption for backup archives: scrypt key derivation + chunked AES-256-GCM.

A backup holds every statement, tax form and balance, so it is encrypted by default. Fernet would
need the whole archive in memory (its token is one message), so this is a small STREAM-style
construction on `cryptography`'s AES-GCM instead: the plaintext is cut into `CHUNK_SIZE` pieces and
each is sealed separately, so memory stays flat however many PDFs the archive holds.

File layout::

    header (38 bytes)  magic | scrypt log2(N), r, p | chunk size | salt (16) | nonce prefix (7)
    chunk 0 .. chunk k  AES-GCM ciphertext + 16-byte tag, `chunk size` plaintext bytes each
                        except the last, which may be shorter

Every chunk's nonce is ``nonce prefix | chunk counter (4 bytes) | last-chunk flag (1 byte)`` and
the whole header is its associated data. So a modified header, a reordered or dropped chunk, and a
file cut off at a chunk boundary (the earlier chunk was sealed as "not last") all fail
authentication. Nothing is returned to the caller before the final chunk has verified: callers
decrypt to a temporary file and only then use it.
"""

from __future__ import annotations

import struct
import unicodedata
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from personal_finance_analyzer.backup.errors import BackupError, BackupFormatError, PassphraseError

MAGIC = b"SPENDENC"

#: scrypt cost: N = 2**17, r = 8 uses about 128 MiB and a few hundred ms. Stored in the header,
#: so it can be raised later without breaking old files; tests lower it.
SCRYPT_LOG2_N = 17
SCRYPT_R = 8
SCRYPT_P = 1
#: A header asking for more than this is rejected rather than honoured, so a hostile file cannot
#: make `restore` allocate without bound. scrypt needs about ``128 * N * r`` bytes, so the per-field
#: bounds alone would still allow 2 GiB (N = 2**20, r = 16); the product is capped at 1 GiB too.
_MAX_LOG2_N = 20
_MAX_R = 16
_MAX_P = 4
_MAX_SCRYPT_BYTES = 1024**3

CHUNK_SIZE = 64 * 1024
_TAG_SIZE = 16
_SALT_SIZE = 16
_NONCE_PREFIX_SIZE = 7

#: magic, log2(N), r, p, chunk size, salt, nonce prefix
_HEADER = struct.Struct(">8sBBBI16s7s")

MIN_PASSPHRASE_LENGTH = 8


def is_encrypted(src: BinaryIO) -> bool:
    """Whether the stream starts with this module's magic. Leaves the position at the start."""
    src.seek(0)
    magic = src.read(len(MAGIC))
    src.seek(0)
    return magic == MAGIC


def validate_new_passphrase(passphrase: str) -> None:
    """Raises `BackupError` if ``passphrase`` is too short to protect a backup."""
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise BackupError(f"the passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters")


def _derive_key(passphrase: str, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    # NFKC so the same passphrase typed on another OS/keyboard layout derives the same key.
    secret = unicodedata.normalize("NFKC", passphrase).encode("utf-8")
    try:
        return Scrypt(salt=salt, length=32, n=2**log2_n, r=r, p=p).derive(secret)
    except (ValueError, MemoryError) as exc:
        raise BackupFormatError(f"cannot derive the key for this backup: {exc}") from exc


def _nonce(prefix: bytes, counter: int, last: bool) -> bytes:
    return prefix + counter.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


def encrypt_stream(src: BinaryIO, dst: BinaryIO, passphrase: str, *, nonce_seed: bytes) -> None:
    """Encrypt ``src`` to ``dst`` under a key derived from ``passphrase``.

    ``nonce_seed`` is 16 + 7 fresh random bytes (salt, then nonce prefix); the caller supplies it
    (`os.urandom`) so tests can pin it and so this module never reads global state.
    """
    validate_new_passphrase(passphrase)
    salt, prefix = nonce_seed[:_SALT_SIZE], nonce_seed[_SALT_SIZE:]
    if len(salt) != _SALT_SIZE or len(prefix) != _NONCE_PREFIX_SIZE:
        raise ValueError("nonce_seed must be 23 bytes")
    header = _HEADER.pack(MAGIC, SCRYPT_LOG2_N, SCRYPT_R, SCRYPT_P, CHUNK_SIZE, salt, prefix)
    aead = AESGCM(_derive_key(passphrase, salt, SCRYPT_LOG2_N, SCRYPT_R, SCRYPT_P))
    dst.write(header)
    counter = 0
    block = src.read(CHUNK_SIZE)
    while True:
        # Read one chunk ahead: whether this one is the last is part of its nonce.
        following = src.read(CHUNK_SIZE) if len(block) == CHUNK_SIZE else b""
        last = not following
        dst.write(aead.encrypt(_nonce(prefix, counter, last), block, header))
        if last:
            return
        block = following
        counter += 1


def decrypt_stream(src: BinaryIO, dst: BinaryIO, passphrase: str) -> None:
    """Decrypt ``src`` to ``dst``; raises `PassphraseError` for a wrong passphrase or a modified
    or truncated file, `BackupFormatError` for a file that is not one of ours.

    ``dst`` may hold partial plaintext when this raises; the caller discards it.
    """
    header = src.read(_HEADER.size)
    if len(header) != _HEADER.size or not header.startswith(MAGIC):
        raise BackupFormatError("not an encrypted Personal Finance Analyzer backup")
    _, log2_n, r, p, chunk_size, salt, prefix = _HEADER.unpack(header)
    if not (1 <= log2_n <= _MAX_LOG2_N and 1 <= r <= _MAX_R and 1 <= p <= _MAX_P):
        raise BackupFormatError("the backup's key-derivation settings are out of range")
    if 128 * (1 << log2_n) * r > _MAX_SCRYPT_BYTES:
        raise BackupFormatError("the backup's key-derivation settings need too much memory")
    if not 1 <= chunk_size <= 16 * 1024 * 1024:
        raise BackupFormatError("the backup's chunk size is out of range")
    aead = AESGCM(_derive_key(passphrase, salt, log2_n, r, p))
    sealed_size = chunk_size + _TAG_SIZE
    counter = 0
    block = src.read(sealed_size)
    while True:
        following = src.read(sealed_size) if len(block) == sealed_size else b""
        last = not following
        try:
            dst.write(aead.decrypt(_nonce(prefix, counter, last), block, header))
        except InvalidTag:
            raise PassphraseError(
                "wrong passphrase, or the backup file is damaged or was modified"
            ) from None
        if last:
            return
        block = following
        counter += 1
