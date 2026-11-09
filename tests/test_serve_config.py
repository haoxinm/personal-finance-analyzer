"""`pfa serve`'s start-up rules for listening beyond loopback (`api/serve_config.py`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from personal_finance_analyzer.api.auth import verify_password
from personal_finance_analyzer.api.serve_config import (
    ServeConfigError,
    is_loopback_host,
    resolve_serve_config,
)
from personal_finance_analyzer.cli import app

runner = CliRunner()
PASSWORD = "correct horse battery"


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.5.5.5", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.10", False),
        ("nas.local", False),
    ],
)
def test_loopback_detection(host: str, loopback: bool) -> None:
    assert is_loopback_host(host) is loopback


def test_default_is_loopback_without_a_login() -> None:
    config = resolve_serve_config(None)
    assert config.host == "127.0.0.1"
    assert config.loopback
    assert config.auth is None
    assert config.allowed_hosts == ()
    assert config.trusted_proxies is None
    assert config.warnings == ()


def test_pfa_host_env_is_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_HOST", "::1")
    assert resolve_serve_config(None).host == "::1"
    assert resolve_serve_config("localhost").host == "localhost"  # the option wins


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])
def test_non_loopback_without_a_password_refuses_to_start(host: str) -> None:
    with pytest.raises(ServeConfigError) as exc_info:
        resolve_serve_config(host)
    message = str(exc_info.value)
    assert "refusing to listen" in message
    assert "PFA_PASSWORD" in message
    assert "PFA_INSECURE_NO_AUTH" in message


def test_non_loopback_with_a_password_enables_the_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    monkeypatch.setenv("PFA_ALLOWED_HOSTS", " NAS.local , 192.168.1.10:9000,,")
    monkeypatch.setenv("PFA_TRUSTED_PROXIES", "172.18.0.0/16")
    monkeypatch.setenv("PFA_COOKIE_SECURE", "1")
    monkeypatch.setenv("PFA_SESSION_IDLE_MINUTES", "30")
    config = resolve_serve_config("0.0.0.0")
    assert config.auth is not None
    assert verify_password(PASSWORD, config.auth.password_hash)
    assert config.auth.force_secure_cookie
    assert config.auth.session_idle_seconds == 1800
    assert config.allowed_hosts == ("nas.local", "192.168.1.10:9000")
    assert config.trusted_proxies == "172.18.0.0/16"
    assert config.warnings == ()
    assert not config.loopback


def test_a_password_on_loopback_still_turns_the_login_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    config = resolve_serve_config("127.0.0.1")
    assert config.auth is not None


def test_insecure_opt_out_warns_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_INSECURE_NO_AUTH", "1")
    config = resolve_serve_config("0.0.0.0")
    assert config.auth is None
    assert any("NO LOGIN" in w for w in config.warnings)
    assert any("PFA_ALLOWED_HOSTS" in w for w in config.warnings)


def test_insecure_flag_is_ignored_when_a_password_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_INSECURE_NO_AUTH", "1")
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    config = resolve_serve_config("0.0.0.0")
    assert config.auth is not None
    assert any("ignored" in w for w in config.warnings)


def test_no_allowed_hosts_beyond_loopback_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    config = resolve_serve_config("0.0.0.0")
    assert any("PFA_ALLOWED_HOSTS is not set" in w for w in config.warnings)


def test_wildcard_host_needs_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_ALLOWED_HOSTS", "*")
    monkeypatch.setenv("PFA_INSECURE_NO_AUTH", "1")
    with pytest.raises(ServeConfigError, match="only allowed together with a password"):
        resolve_serve_config("0.0.0.0")
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    assert resolve_serve_config("0.0.0.0").allowed_hosts == ("*",)


@pytest.mark.parametrize("value", ["http://nas.local", "nas.local/path", "two words"])
def test_allowed_hosts_must_be_bare_names(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PFA_ALLOWED_HOSTS", value)
    with pytest.raises(ServeConfigError, match="bare host names"):
        resolve_serve_config(None)


@pytest.mark.parametrize("value", ["0", "-5", "abc", "99999999"])
def test_session_idle_must_be_sane(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    monkeypatch.setenv("PFA_SESSION_IDLE_MINUTES", value)
    with pytest.raises(ServeConfigError, match="PFA_SESSION_IDLE_MINUTES"):
        resolve_serve_config(None)


def test_password_problems_are_start_up_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_PASSWORD", "short")
    with pytest.raises(ServeConfigError, match="at least 8"):
        resolve_serve_config(None)


# --- through the CLI ----------------------------------------------------------------------


def test_cli_serve_refuses_a_public_bind_without_a_password() -> None:
    result = runner.invoke(app, ["serve", "--no-open-browser", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "refusing to listen on 0.0.0.0" in result.output


def test_cli_serve_refuses_a_bad_journal_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_DB_JOURNAL_MODE", "bogus")
    result = runner.invoke(app, ["serve", "--no-open-browser"])
    assert result.exit_code == 2
    assert "PFA_DB_JOURNAL_MODE" in result.output


def test_cli_serve_passes_the_settings_to_the_app_and_uvicorn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import uvicorn

    calls: dict[str, Any] = {}

    def fake_run(application: object, **kwargs: Any) -> None:
        calls["app"] = application
        calls["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setenv("PFA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    monkeypatch.setenv("PFA_ALLOWED_HOSTS", "nas.local")
    monkeypatch.setenv("PFA_TRUSTED_PROXIES", "10.0.0.5")
    assert runner.invoke(app, ["migrate"]).exit_code == 0  # `serve` itself does not migrate
    result = runner.invoke(app, ["serve", "--no-open-browser", "--host", "0.0.0.0"])
    assert result.exit_code == 0, result.output
    assert calls["kwargs"]["host"] == "0.0.0.0"
    # Forwarding headers are read by the app's own ClientGuardMiddleware (it can fail closed),
    # never by uvicorn.
    assert calls["kwargs"]["proxy_headers"] is False
    assert "forwarded_allow_ips" not in calls["kwargs"]
    assert any(m.cls.__name__ == "ClientGuardMiddleware" for m in calls["app"].user_middleware)
    assert "Accepting connections only from: private (" in result.output
    assert calls["app"].state.auth is not None
    # The per-launch token must not be printed where a container log would expose it.
    assert calls["app"].state.token not in result.output
    assert "password login on" in result.output


def test_cli_serve_default_is_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import uvicorn

    calls: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda application, **kw: calls.update(kw, app=application))
    monkeypatch.setenv("PFA_HOME", str(tmp_path / "home"))
    assert runner.invoke(app, ["migrate"]).exit_code == 0
    result = runner.invoke(app, ["serve", "--no-open-browser"])
    assert result.exit_code == 0, result.output
    assert calls["host"] == "127.0.0.1"
    assert calls["proxy_headers"] is False
    assert not hasattr(calls["app"].state, "auth")
    assert not any(m.cls.__name__ == "ClientGuardMiddleware" for m in calls["app"].user_middleware)
    assert "Accepting connections only" not in result.output
    assert f"/?token={calls['app'].state.token}" in result.output
    assert f"Per-launch API token: {calls['app'].state.token}" in result.output


def test_cli_hash_password_round_trip() -> None:
    result = runner.invoke(app, ["auth", "hash-password", "--stdin"], input=PASSWORD + "\n")
    assert result.exit_code == 0
    encoded = result.output.strip()
    assert verify_password(PASSWORD, encoded)
    assert PASSWORD not in result.output


def test_cli_hash_password_refuses_a_short_password() -> None:
    result = runner.invoke(app, ["auth", "hash-password", "--stdin"], input="short\n")
    assert result.exit_code == 1
    assert "at least 8" in result.output


# --- LAN-only: PFA_ALLOWED_CLIENTS, PFA_ALLOW_PUBLIC_CLIENTS, PFA_TRUSTED_PROXIES -----------------


def test_beyond_loopback_the_client_check_defaults_to_private_networks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    monkeypatch.setenv("PFA_ALLOWED_HOSTS", "nas.local")
    config = resolve_serve_config("0.0.0.0")
    assert config.client_policy is not None
    assert config.client_policy.allows("192.168.1.20")
    assert not config.client_policy.allows("8.8.8.8")
    assert not config.client_policy.allows("100.64.0.9")  # CGNAT is opt-in
    assert config.warnings == ()


def test_loopback_bind_has_no_client_check(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_serve_config(None).client_policy is None
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    assert resolve_serve_config("127.0.0.1").client_policy is None


def test_a_reverse_proxy_in_front_of_a_loopback_bind_turns_the_check_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    monkeypatch.setenv("PFA_TRUSTED_PROXIES", "127.0.0.1")
    config = resolve_serve_config("127.0.0.1")
    assert config.client_policy is not None
    assert config.proxies is not None


def test_allowed_clients_replaces_the_default_and_understands_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    monkeypatch.setenv("PFA_ALLOWED_CLIENTS", "192.168.1.0/24")
    only_lan = resolve_serve_config("0.0.0.0").client_policy
    assert only_lan is not None
    assert only_lan.allows("192.168.1.7")
    assert not only_lan.allows("192.168.2.7")
    assert not only_lan.allows("10.0.0.1")

    monkeypatch.setenv("PFA_ALLOWED_CLIENTS", "private, 100.64.0.0/10")
    plus_tailscale = resolve_serve_config("0.0.0.0").client_policy
    assert plus_tailscale is not None
    assert plus_tailscale.allows("10.0.0.1")
    assert plus_tailscale.allows("100.101.102.103")
    assert not plus_tailscale.allows("8.8.8.8")


@pytest.mark.parametrize("value", ["nonsense", "192.168.1.0/99", "0.0.0.0/0", "::/0", "0.0.0.0/4"])
def test_allowed_clients_refuses_garbage_and_everyone(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    monkeypatch.setenv("PFA_ALLOWED_CLIENTS", value)
    with pytest.raises(ServeConfigError, match="PFA_ALLOWED_CLIENTS"):
        resolve_serve_config("0.0.0.0")


def test_trusted_proxies_must_be_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    monkeypatch.setenv("PFA_TRUSTED_PROXIES", "traefik")
    with pytest.raises(ServeConfigError, match="PFA_TRUSTED_PROXIES"):
        resolve_serve_config("0.0.0.0")


def test_allow_public_clients_switches_the_check_off_with_a_loud_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    monkeypatch.setenv("PFA_ALLOW_PUBLIC_CLIENTS", "1")
    monkeypatch.setenv("PFA_ALLOWED_CLIENTS", "192.168.1.0/24")
    config = resolve_serve_config("0.0.0.0")
    assert config.client_policy is None
    joined = " ".join(config.warnings)
    assert "PFA_ALLOW_PUBLIC_CLIENTS=1" in joined
    assert "OFF" in joined
    assert "internet" in joined
    assert "PFA_ALLOWED_CLIENTS is ignored" in joined


@pytest.mark.parametrize("value", ["yes please", "public", "0", ""])
def test_allow_public_clients_needs_an_explicit_true(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    monkeypatch.setenv("PFA_ALLOW_PUBLIC_CLIENTS", value)
    assert resolve_serve_config("0.0.0.0").client_policy is not None


def test_allow_public_clients_is_refused_without_a_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_INSECURE_NO_AUTH", "1")
    monkeypatch.setenv("PFA_ALLOW_PUBLIC_CLIENTS", "1")
    with pytest.raises(ServeConfigError, match="whole internet"):
        resolve_serve_config("0.0.0.0")


def test_wildcard_trusted_proxies_warns_that_it_defeats_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    monkeypatch.setenv("PFA_TRUSTED_PROXIES", "*")
    config = resolve_serve_config("0.0.0.0")
    assert any("PFA_TRUSTED_PROXIES=*" in w for w in config.warnings)


def test_cli_hash_password_prompts_on_stderr_so_redirecting_stdout_captures_only_the_hash() -> None:
    result = runner.invoke(app, ["auth", "hash-password"], input=f"{PASSWORD}\n{PASSWORD}\n")
    assert result.exit_code == 0, result.output
    assert verify_password(PASSWORD, result.stdout.strip())  # nothing but the hash on stdout
    assert "Password" in result.stderr
