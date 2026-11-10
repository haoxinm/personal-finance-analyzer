"""Detect a missing or stale built web UI when running from a source checkout."""

from __future__ import annotations

from pathlib import Path

NOT_BUILT_MESSAGE = "Web UI not built — run: cd frontend && npm ci && npm run build"
STALE_MESSAGE = (
    "Web UI build is older than frontend sources — run: cd frontend && npm run build, "
    "then restart serve"
)


def _newest_source_mtime(frontend_dir: Path) -> float:
    newest = 0.0
    for single in (frontend_dir / "index.html", frontend_dir / "package.json"):
        try:
            newest = max(newest, single.stat().st_mtime)
        except OSError:
            continue
    src = frontend_dir / "src"
    if src.is_dir():
        for path in src.rglob("*"):
            try:
                if path.is_file():
                    newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
    return newest


def web_ui_warning(frontend_dir: Path, web_dir: Path) -> str | None:
    """Return a warning when the built UI is missing/stale, else None. Never raises.

    Returns None when `frontend_dir` has no package.json (an installed wheel)."""
    try:
        if not (frontend_dir / "package.json").is_file():
            return None
        index = web_dir / "index.html"
        if not index.is_file():
            return NOT_BUILT_MESSAGE
        if _newest_source_mtime(frontend_dir) > index.stat().st_mtime:
            return STALE_MESSAGE
    except OSError:
        return None
    return None
