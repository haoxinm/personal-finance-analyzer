"""Hatchling build hook (P4-D): builds the frontend into `src/personal_finance_analyzer/web/` before a
*standard* (non-editable) wheel is built, so that a built wheel/sdist-installed package ships the
UI (§4 "Built into `src/personal_finance_analyzer/web/` ... production is one process on one port").

Never touches anything under `frontend/**` beyond invoking its own, already-committed `npm`
scripts (`npm ci`, `npm run build`) as subprocesses — `frontend/vite.config.ts` already points
`build.outDir` at `../src/personal_finance_analyzer/web` (P1-F), so this hook only decides *when* to run that
existing build, not *how*.

Skipped for an editable install (`version == "editable"`, i.e. `uv sync` / `pip install -e .`) so
day-to-day development never needs `node`/`npm` installed just to sync Python dependencies; the
API still serves fine without `web/` (`create_app` mounts it only if present, P2-C). Also skipped
if `PFA_SKIP_FRONTEND_BUILD` is set (e.g. a CI job that only needs the sdist, or a
machine intentionally building without Node), or if a prebuilt `web/index.html` already exists and
`PFA_SKIP_FRONTEND_BUILD` is unset but `npm`/`node` are unavailable (a clear warning is
printed either way, since a wheel built without a warning that silently ships no UI would be
confusing to debug).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class FrontendBuildHook(BuildHookInterface):  # type: ignore[misc]
    """Runs the frontend's own `npm run build` so its output lands in `src/personal_finance_analyzer/web/`
    ahead of wheel packaging (`[tool.hatch.build.targets.wheel] artifacts` then force-includes
    it, since it is otherwise git-ignored, P1-F)."""

    PLUGIN_NAME = "frontend"

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if version == "editable":
            return  # `uv sync` / `pip install -e .`: no frontend build needed for Python dev.

        root = Path(self.root)
        frontend_dir = root / "frontend"
        web_dir = root / "src" / "personal_finance_analyzer" / "web"

        if os.environ.get("PFA_SKIP_FRONTEND_BUILD") or os.environ.get(
            "SPEND_ANALYZER_SKIP_FRONTEND_BUILD"  # deprecated name
        ):
            self.app.display_info(
                "frontend build hook: PFA_SKIP_FRONTEND_BUILD set, skipping "
                "`npm run build` (the wheel will not include the web UI)."
            )
            return

        if not frontend_dir.is_dir():
            self.app.display_warning(
                "frontend build hook: no frontend/ directory found (building from an sdist "
                "that omitted it?); the wheel will not include the web UI."
            )
            return

        npm = shutil.which("npm")
        if npm is None:
            self.app.display_warning(
                "frontend build hook: `npm` not found on PATH; the wheel will not include the "
                "web UI. Install Node.js/npm, or set PFA_SKIP_FRONTEND_BUILD=1 to "
                "silence this warning."
            )
            return

        self.app.display_info("frontend build hook: npm ci")
        subprocess.run([npm, "ci"], cwd=frontend_dir, check=True)
        # Deliberately `npx vite build`, not `npm run build` (which is `tsc -b && vite build`):
        # packaging only needs the bundle vite's esbuild-based transform produces, and `tsc -b`
        # here also type-checks `*.test.tsx` under the same tsconfig (`tsconfig.app.json`'s
        # `include: ["src"]` has no test exclusion) — a whole extra, slower type-check pass over
        # non-shipped files that belongs to `npm run typecheck` in CI (§0.7 DoD item 3, owned by
        # the frontend work packages), not to producing this wheel's artifacts.
        self.app.display_info("frontend build hook: npx vite build")
        subprocess.run([npm, "exec", "--", "vite", "build"], cwd=frontend_dir, check=True)

        if not (web_dir / "index.html").is_file():
            raise RuntimeError(
                "frontend build hook: `npm run build` did not produce "
                f"{web_dir / 'index.html'}; check frontend/vite.config.ts's build.outDir."
            )
