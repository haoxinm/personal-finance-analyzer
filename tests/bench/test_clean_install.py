"""P4-D deliverable (1): build a wheel and verify it installs and runs cleanly outside the repo.

Opt-in only (`@pytest.mark.benchmark`; not a performance budget, but this is by far the slowest
check in the suite — it builds the frontend, builds a wheel, creates a throwaway virtualenv
*outside the repository*, `pip install`s the wheel into it, and drives `migrate`/`doctor`/`serve`
against a temp `PFA_HOME` — so it belongs with the rest of the opt-in suite, run with::

    uv run pytest -m benchmark tests/bench/test_clean_install.py -v -s

This is the one test in the repository that legitimately reaches the network (`pip install`
resolving this package's runtime dependencies into a *fresh* venv) and shells out to `uv`/`python`
subprocesses; §6.5's "zero network calls in CI" is about the always-on suite, which never collects
this module (excluded by the default `-m "not benchmark"`, `pyproject.toml`)."""

from __future__ import annotations

import shutil
import subprocess
import time
import venv
from pathlib import Path

import pytest

pytestmark = pytest.mark.benchmark

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVER_PORT = 8756


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=cwd, env=env)


def test_wheel_builds_and_clean_installs_and_serves(tmp_path: Path) -> None:
    if shutil.which("npm") is None:
        pytest.skip("npm not on PATH; the frontend build hook needs it to produce a full wheel")
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")

    dist_dir = tmp_path / "dist"
    build = _run(["uv", "build", "--wheel", "-o", str(dist_dir)], cwd=_REPO_ROOT)
    assert build.returncode == 0, f"uv build failed:\n{build.stdout}\n{build.stderr}"

    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, found {wheels}"
    wheel = wheels[0]

    # A genuinely fresh venv, outside the repository (§ deliverable 1: "outside the repo").
    venv_dir = tmp_path / "clean-venv"
    venv.create(venv_dir, with_pip=True)
    pip = venv_dir / "bin" / "pip"
    pfa_bin = venv_dir / "bin" / "pfa"

    install = _run([str(pip), "install", "--quiet", str(wheel)])
    assert install.returncode == 0, f"pip install failed:\n{install.stdout}\n{install.stderr}"

    home = tmp_path / "pfa_home"
    home.mkdir()
    env = {"PFA_HOME": str(home), "PATH": f"{venv_dir / 'bin'}:/usr/bin:/bin"}

    migrate = _run([str(pfa_bin), "migrate"], env=env)
    assert migrate.returncode == 0, f"migrate failed:\n{migrate.stdout}\n{migrate.stderr}"

    doctor = _run([str(pfa_bin), "doctor"], env=env)
    # Keychain access commonly fails in a headless container with no secret-service daemon
    # (unrelated to packaging); every other line must PASS.
    for line in doctor.stdout.splitlines():
        if line.startswith("FAIL") and "Keychain" not in line:
            pytest.fail(f"doctor reported an unexpected failure: {line}\n{doctor.stdout}")

    server = subprocess.Popen(
        [str(pfa_bin), "serve", "--no-open-browser"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        token = _wait_for_token(server)
        assert token is not None, "server never printed a per-launch token"

        # The token is echoed before `uvicorn.run()` is even called (`cli.py`'s `serve` command),
        # so the socket may not be accepting connections yet; retry briefly rather than racing it.
        body = _get_with_retry(f"http://127.0.0.1:{_SERVER_PORT}/", token)
        assert f'content="{token}"' in body, "index.html did not carry the per-launch token"

        _get_with_retry(f"http://127.0.0.1:{_SERVER_PORT}/api/settings", token)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def _get_with_retry(url: str, token: str, *, timeout_s: float = 10.0) -> str:
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, headers={"X-PFA-Token": token})
            with urllib.request.urlopen(req, timeout=2) as resp:
                assert resp.status == 200
                return str(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError) as exc:
            last_error = exc
            time.sleep(0.2)
    raise AssertionError(f"{url} never became reachable: {last_error}")


def _wait_for_token(server: subprocess.Popen[str], *, timeout_s: float = 15.0) -> str | None:
    assert server.stdout is not None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = server.stdout.readline()
        if not line:
            time.sleep(0.1)
            continue
        if line.startswith("Per-launch API token:"):
            return str(line.split(":", 1)[1].strip())
    return None
