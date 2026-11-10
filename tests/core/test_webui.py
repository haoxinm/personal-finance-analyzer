from __future__ import annotations

import os
from pathlib import Path

from personal_finance_analyzer.core.webui import NOT_BUILT_MESSAGE, STALE_MESSAGE, web_ui_warning


def _checkout(tmp_path: Path) -> tuple[Path, Path]:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "package.json").write_text("{}")
    (frontend / "index.html").write_text("x")
    (frontend / "src" / "main.ts").write_text("x")
    web = tmp_path / "web"
    web.mkdir()
    return frontend, web


def _set_mtime(path: Path, t: float) -> None:
    os.utime(path, (t, t))


def test_no_frontend_dir_skips(tmp_path: Path) -> None:
    assert web_ui_warning(tmp_path / "frontend", tmp_path / "web") is None


def test_missing_build(tmp_path: Path) -> None:
    frontend, web = _checkout(tmp_path)
    assert web_ui_warning(frontend, web) == NOT_BUILT_MESSAGE


def test_stale_and_fresh(tmp_path: Path) -> None:
    frontend, web = _checkout(tmp_path)
    index = web / "index.html"
    index.write_text("built")
    for f in (frontend / "package.json", frontend / "index.html", frontend / "src" / "main.ts"):
        _set_mtime(f, 1000)
    _set_mtime(index, 2000)
    assert web_ui_warning(frontend, web) is None
    _set_mtime(frontend / "src" / "main.ts", 3000)
    assert web_ui_warning(frontend, web) == STALE_MESSAGE
    _set_mtime(frontend / "src" / "main.ts", 1000)
    _set_mtime(frontend / "package.json", 3000)
    assert web_ui_warning(frontend, web) == STALE_MESSAGE
