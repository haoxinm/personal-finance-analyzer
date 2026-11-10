from __future__ import annotations

import pytest

from personal_finance_analyzer.core import shellquote


def test_posix_quotes_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shellquote, "is_windows", lambda: False)
    cmd = shellquote.join_command(["pfa", "import", "/tmp/my dir/a.pdf", "--user-id", "1"])
    assert cmd == "pfa import '/tmp/my dir/a.pdf' --user-id 1"


def test_windows_path_with_backslashes_and_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shellquote, "is_windows", lambda: True)
    path = r"D:\a\my repo\tests\fixtures\x.pdf"
    cmd = shellquote.join_command(["pfa", "import", path, "--user-id", "1"])
    assert cmd == f'pfa import "{path}" --user-id 1'


def test_windows_plain_path_unquoted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shellquote, "is_windows", lambda: True)
    assert shellquote.quote_arg(r"D:\a\x.pdf") == r"D:\a\x.pdf"
