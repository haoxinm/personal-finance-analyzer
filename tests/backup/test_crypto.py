from __future__ import annotations

import io
import os

import pytest

from spend_analyzer.backup import crypto
from spend_analyzer.backup.errors import BackupError, BackupFormatError, PassphraseError

PASS = "a long enough passphrase"
SEED = bytes(range(23))


def _seal(data: bytes, passphrase: str = PASS) -> bytes:
    out = io.BytesIO()
    crypto.encrypt_stream(io.BytesIO(data), out, passphrase, nonce_seed=SEED)
    return out.getvalue()


def _open(blob: bytes, passphrase: str = PASS) -> bytes:
    out = io.BytesIO()
    crypto.decrypt_stream(io.BytesIO(blob), out, passphrase)
    return out.getvalue()


@pytest.mark.parametrize(
    "size",
    [0, 1, crypto.CHUNK_SIZE - 1, crypto.CHUNK_SIZE, crypto.CHUNK_SIZE + 1, 3 * crypto.CHUNK_SIZE],
)
def test_round_trip_at_chunk_boundaries(size: int) -> None:
    data = os.urandom(size)
    assert _open(_seal(data)) == data


def test_ciphertext_does_not_contain_plaintext() -> None:
    data = b"very recognisable plaintext " * 100
    assert b"recognisable" not in _seal(data)


def test_wrong_passphrase_fails() -> None:
    with pytest.raises(PassphraseError):
        _open(_seal(b"secret"), "another passphrase!")


def test_flipped_byte_in_any_chunk_fails() -> None:
    blob = bytearray(_seal(os.urandom(2 * crypto.CHUNK_SIZE + 10)))
    blob[-5] ^= 0x01
    with pytest.raises(PassphraseError):
        _open(bytes(blob))


def test_header_is_authenticated() -> None:
    blob = bytearray(_seal(b"data"))
    blob[len(crypto.MAGIC) + 3 + 4 + 2] ^= 0x01  # a salt byte: derives a different key
    with pytest.raises(PassphraseError):
        _open(bytes(blob))


def test_truncation_at_a_chunk_boundary_fails() -> None:
    sealed_chunk = crypto.CHUNK_SIZE + 16
    blob = _seal(os.urandom(3 * crypto.CHUNK_SIZE))
    header = len(blob) - 3 * sealed_chunk
    with pytest.raises(PassphraseError):
        _open(blob[: header + 2 * sealed_chunk])


def test_reordered_chunks_fail() -> None:
    sealed_chunk = crypto.CHUNK_SIZE + 16
    blob = _seal(os.urandom(3 * crypto.CHUNK_SIZE))
    header = len(blob) - 3 * sealed_chunk
    body = blob[header:]
    swapped = body[sealed_chunk : 2 * sealed_chunk] + body[:sealed_chunk] + body[2 * sealed_chunk :]
    with pytest.raises(PassphraseError):
        _open(blob[:header] + swapped)


def test_not_an_encrypted_file() -> None:
    with pytest.raises(BackupFormatError):
        _open(b"PK\x03\x04 plain zip bytes, definitely not ours")


def test_empty_ciphertext_fails() -> None:
    with pytest.raises(PassphraseError):
        _open(_seal(b"x")[: crypto._HEADER.size])


def test_decrypt_reads_cost_from_header_not_module_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob = _seal(b"payload")
    monkeypatch.setattr(crypto, "SCRYPT_LOG2_N", 12)
    assert _open(blob) == b"payload"


def test_absurd_kdf_cost_in_header_is_rejected() -> None:
    blob = bytearray(_seal(b"payload"))
    blob[len(crypto.MAGIC)] = 40  # log2(N): would need terabytes
    with pytest.raises(BackupFormatError):
        _open(bytes(blob))


def test_short_passphrase_rejected() -> None:
    with pytest.raises(BackupError):
        _seal(b"x", "short")


def test_is_encrypted_detects_magic() -> None:
    assert crypto.is_encrypted(io.BytesIO(_seal(b"x")))
    assert not crypto.is_encrypted(io.BytesIO(b"PK\x03\x04"))
