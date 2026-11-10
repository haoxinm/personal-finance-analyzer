"""Tests for `spend_analyzer.ingest.extract` (P1-A)."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.pdfgen import canvas

from spend_analyzer.config import PrivacyConfig, Settings, save_settings
from spend_analyzer.core.errors import ParserError
from spend_analyzer.core.paths import extract_cache_dir
from spend_analyzer.ingest.extract import extract, sha256_file
from tests.fixtures.gen.base import render_lines_pdf

# --------------------------------------------------------------------------------------------
# sha256_file
# --------------------------------------------------------------------------------------------


def test_sha256_file_matches_known_digest(tmp_path: Path) -> None:
    path = tmp_path / "hello.txt"
    path.write_bytes(b"hello world")
    # sha256("hello world"), a well-known test vector.
    assert sha256_file(path) == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_sha256_file_is_deterministic_and_content_sensitive(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"same content")
    b.write_bytes(b"same content")
    c = tmp_path / "c.txt"
    c.write_bytes(b"different content")

    assert sha256_file(a) == sha256_file(b)
    assert sha256_file(a) != sha256_file(c)
    assert sha256_file(a) == sha256_file(a)  # stable across repeated calls


def test_sha256_file_large_file_chunked_read(tmp_path: Path) -> None:
    # Larger than the internal chunk size, to exercise the chunked read loop.
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (3 * 1024 * 1024 + 17))
    digest = sha256_file(path)
    assert len(digest) == 64
    int(digest, 16)  # valid hex


# --------------------------------------------------------------------------------------------
# extract() — text-bearing PDF
# --------------------------------------------------------------------------------------------


def _text_pdf(tmp_path: Path, name: str = "statement.pdf") -> Path:
    path = tmp_path / name
    pages = [
        [
            "STATEMENT OF ACCOUNT",
            "05/01/2026  GROCERY STORE               45.67",
            "05/02/2026  COFFEE SHOP                  4.50",
        ],
        [
            "05/03/2026  ONLINE RETAILER            123.45",
        ],
    ]
    render_lines_pdf(path, pages)
    return path


def test_extract_reports_correct_page_count(tmp_path: Path) -> None:
    doc = extract(_text_pdf(tmp_path))
    assert doc.page_count == 2
    assert len(doc.pages) == 2
    assert [p.page_number for p in doc.pages] == [1, 2]


def test_extract_has_text_layer_true_for_text_pdf(tmp_path: Path) -> None:
    doc = extract(_text_pdf(tmp_path))
    assert doc.has_text_layer is True


def test_extract_recovers_word_coordinates(tmp_path: Path) -> None:
    doc = extract(_text_pdf(tmp_path))
    first_page = doc.pages[0]
    assert first_page.words, "expected extract_words() to return word boxes"
    first_word = first_page.words[0]
    assert first_word.text == "STATEMENT"
    assert first_word.x0 < first_word.x1
    assert first_word.bottom > first_word.top


def test_extract_char_count_matches_text_length(tmp_path: Path) -> None:
    doc = extract(_text_pdf(tmp_path))
    for page in doc.pages:
        assert page.char_count == len(page.text)


def test_extract_full_text_joins_pages_with_form_feed(tmp_path: Path) -> None:
    doc = extract(_text_pdf(tmp_path))
    assert "\n\f\n" in doc.full_text


def test_extract_file_sha256_matches_sha256_file(tmp_path: Path) -> None:
    path = _text_pdf(tmp_path)
    doc = extract(path)
    assert doc.file_sha256 == sha256_file(path)


def test_extract_is_deterministic_across_calls(tmp_path: Path) -> None:
    path = _text_pdf(tmp_path)
    first = extract(path)
    second = extract(path)
    assert first.file_sha256 == second.file_sha256
    assert [p.text for p in first.pages] == [p.text for p in second.pages]


# --------------------------------------------------------------------------------------------
# extract() — image-only / no text layer
# --------------------------------------------------------------------------------------------


def _image_only_pdf(tmp_path: Path, name: str = "scanned.pdf") -> Path:
    """A PDF with drawn graphics but no text objects: pdfplumber's `extract_text()` returns
    `""` for every page, exactly as it would for a scanned/rasterized page with no text layer."""
    path = tmp_path / name
    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.rect(50, 50, 400, 600, fill=1)
    pdf.showPage()
    pdf.save()
    return path


def test_extract_reports_no_text_layer_for_image_only_pdf(tmp_path: Path) -> None:
    doc = extract(_image_only_pdf(tmp_path))
    assert doc.has_text_layer is False
    assert doc.page_count == 1


def test_extract_no_text_layer_does_not_raise(tmp_path: Path) -> None:
    # extract() reports has_text_layer=False; it is the caller's job to raise/translate it.
    doc = extract(_image_only_pdf(tmp_path))
    assert doc is not None


def test_extract_text_layer_threshold_uses_median_not_mean(tmp_path: Path) -> None:
    """One dense page and several blank pages: the median must not be dragged above threshold
    by a single outlier page, and must not be dragged below it either."""
    path = tmp_path / "mixed.pdf"
    dense_line = "05/01/2026  A REASONABLY LONG MERCHANT DESCRIPTION LINE HERE    123.45"
    assert len(dense_line) >= 50
    render_lines_pdf(path, [[dense_line], [], [], []])
    doc = extract(path)
    assert doc.page_count == 4
    # Median of [len(dense_line), 0, 0, 0] is 0 -> below threshold.
    assert doc.has_text_layer is False


# --------------------------------------------------------------------------------------------
# extract() — encrypted PDF
# --------------------------------------------------------------------------------------------


def _encrypted_pdf(tmp_path: Path, name: str = "protected.pdf") -> Path:
    path = tmp_path / name
    enc = StandardEncryption("s3cret-user-pw", ownerPassword="s3cret-owner-pw", canPrint=1)
    pdf = canvas.Canvas(str(path), pagesize=letter, encrypt=enc)
    pdf.setFont("Courier", 9)
    pdf.drawString(50, 700, "SHOULD NOT BE READABLE WITHOUT A PASSWORD")
    pdf.showPage()
    pdf.save()
    return path


def test_extract_raises_parser_error_for_password_protected_pdf(tmp_path: Path) -> None:
    with pytest.raises(ParserError, match="password-protected"):
        extract(_encrypted_pdf(tmp_path))


def test_extract_password_protected_error_message_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(ParserError) as exc_info:
        extract(_encrypted_pdf(tmp_path))
    message = str(exc_info.value)
    assert "password" in message.lower()
    # The message should tell the user what to do next, not just what went wrong.
    assert "re-upload" in message.lower() or "remove" in message.lower()


def test_extract_raises_parser_error_for_corrupt_pdf(tmp_path: Path) -> None:
    path = tmp_path / "not-a-pdf.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not a valid pdf body at all")
    with pytest.raises(ParserError):
        extract(path)


# --------------------------------------------------------------------------------------------
# No network (I3)
# --------------------------------------------------------------------------------------------


def test_extract_never_opens_a_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("extract() must never open a socket")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)

    doc = extract(_text_pdf(tmp_path))
    assert doc.page_count == 2


# --------------------------------------------------------------------------------------------
# Debug extract cache (off by default; gated on [privacy] store_extract_cache)
# --------------------------------------------------------------------------------------------


def test_extract_does_not_write_debug_cache_by_default(tmp_path: Path, home: Path) -> None:
    doc = extract(_text_pdf(tmp_path))
    cache_file = extract_cache_dir() / f"{doc.file_sha256}.txt"
    assert not cache_file.exists()


def test_extract_writes_debug_cache_when_enabled(tmp_path: Path, home: Path) -> None:
    save_settings(Settings(privacy=PrivacyConfig(store_extract_cache=True)))
    doc = extract(_text_pdf(tmp_path))
    cache_file = extract_cache_dir() / f"{doc.file_sha256}.txt"
    assert cache_file.exists()
    assert cache_file.read_text(encoding="utf-8") == doc.full_text


def test_extract_debug_cache_disabled_explicitly_writes_nothing(tmp_path: Path, home: Path) -> None:
    save_settings(Settings(privacy=PrivacyConfig(store_extract_cache=False)))
    doc = extract(_text_pdf(tmp_path))
    cache_file = extract_cache_dir() / f"{doc.file_sha256}.txt"
    assert not cache_file.exists()


def test_extract_write_cache_false_skips_cache_even_when_enabled(
    tmp_path: Path, home: Path
) -> None:
    save_settings(Settings(privacy=PrivacyConfig(store_extract_cache=True)))
    doc = extract(_text_pdf(tmp_path), write_cache=False)
    cache_file = extract_cache_dir() / f"{doc.file_sha256}.txt"
    assert not cache_file.exists()


# --------------------------------------------------------------------------------------------
# Performance budget
# --------------------------------------------------------------------------------------------


def test_extract_twelve_page_statement_under_two_seconds(tmp_path: Path) -> None:
    import time

    path = tmp_path / "twelve_pages.pdf"
    page = [f"05/{i:02d}/2026  MERCHANT NUMBER {i:03d}                {i}.99" for i in range(1, 40)]
    render_lines_pdf(path, [page] * 12)

    start = time.monotonic()
    doc = extract(path)
    elapsed = time.monotonic() - start

    assert doc.page_count == 12
    # 2 s is what a developer machine needs. Shared CI runners are noisy (one ubuntu run took
    # 2.42 s for the same work), so give them a wider budget; it still catches a real regression.
    budget = 6.0 if os.environ.get("CI") else 2.0
    assert elapsed < budget
