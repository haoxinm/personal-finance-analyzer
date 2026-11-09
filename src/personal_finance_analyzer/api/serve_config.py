"""What `pfa serve` needs to know about where it listens and who may reach it.

Everything here comes from the command line and the environment (deployment settings, kept out of
`config.toml` so the Settings screen can never loosen them):

- ``PFA_HOST``: address to bind (default ``127.0.0.1``; the Docker image uses ``0.0.0.0``).
- ``PFA_PASSWORD[_FILE]`` or ``PFA_PASSWORD_HASH[_FILE]``: the login (see `api/auth.py`).
- ``PFA_INSECURE_NO_AUTH``: ``1`` to listen beyond loopback with no login (loud warning).
- ``PFA_ALLOWED_HOSTS``: comma list of extra ``Host`` names, e.g. ``nas.local,192.168.1.10:9000``,
  or ``*`` (with a password only).
- ``PFA_TRUSTED_PROXIES``: addresses or CIDRs whose ``X-Forwarded-For/Proto`` headers are believed
  (or ``*``); needed for a ``Secure`` cookie and per-client login limits behind a reverse proxy.
- ``PFA_COOKIE_SECURE``: ``1`` to mark the session cookie ``Secure`` even over plain HTTP.
- ``PFA_SESSION_IDLE_MINUTES``: idle session timeout (default 480).

Rule: a loopback bind needs nothing (a password, if set, still turns the login on). A bind
beyond loopback needs a password or ``PFA_INSECURE_NO_AUTH=1``, otherwise the server refuses to
start.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass

from personal_finance_analyzer.api.auth import AuthConfig, AuthConfigError, load_password_hash
from personal_finance_analyzer.core.container import env_flag
from personal_finance_analyzer.core.errors import PfaError

HOST_ENV = "PFA_HOST"
ALLOWED_HOSTS_ENV = "PFA_ALLOWED_HOSTS"
TRUSTED_PROXIES_ENV = "PFA_TRUSTED_PROXIES"
INSECURE_ENV = "PFA_INSECURE_NO_AUTH"
COOKIE_SECURE_ENV = "PFA_COOKIE_SECURE"
SESSION_IDLE_ENV = "PFA_SESSION_IDLE_MINUTES"

DEFAULT_HOST = "127.0.0.1"


class ServeConfigError(PfaError):
    """The server cannot start with this configuration. The message says what to set."""


@dataclass(frozen=True)
class ServeConfig:
    host: str
    #: None: no login (loopback without a password, or the explicit insecure opt-out).
    auth: AuthConfig | None
    allowed_hosts: tuple[str, ...]
    #: Passed to uvicorn's ``forwarded_allow_ips``; None leaves proxy headers untrusted.
    trusted_proxies: str | None
    #: Printed at startup, in order.
    warnings: tuple[str, ...]

    @property
    def loopback(self) -> bool:
        return is_loopback_host(self.host)


def is_loopback_host(host: str) -> bool:
    """Whether binding ``host`` keeps the server reachable from this machine only."""
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _parse_allowed_hosts(raw: str) -> tuple[str, ...]:
    entries = []
    for part in raw.split(","):
        entry = part.strip().lower()
        if not entry:
            continue
        if "://" in entry or "/" in entry or " " in entry:
            raise ServeConfigError(
                f"{ALLOWED_HOSTS_ENV} entries are bare host names like nas.local or "
                f"192.168.1.10:9000, not URLs (got {part.strip()!r})"
            )
        entries.append(entry)
    return tuple(entries)


def _idle_minutes() -> int:
    raw = os.environ.get(SESSION_IDLE_ENV, "").strip()
    if not raw:
        return 480
    try:
        minutes = int(raw)
    except ValueError:
        minutes = 0
    if not 1 <= minutes <= 60 * 24 * 30:
        raise ServeConfigError(f"{SESSION_IDLE_ENV} must be a number of minutes (1 to 43200)")
    return minutes


def resolve_serve_config(host: str | None) -> ServeConfig:
    """Resolve the serve settings from ``host`` (the ``--host`` option; ``PFA_HOST`` is merged in
    by the CLI, and read here as a fallback) and the environment.

    Raises:
        ServeConfigError: a non-loopback bind without a password (and without
            ``PFA_INSECURE_NO_AUTH``), or an invalid setting.
    """
    bind = (host or os.environ.get(HOST_ENV, "") or DEFAULT_HOST).strip()
    loopback = is_loopback_host(bind)

    try:
        password_hash = load_password_hash()
    except AuthConfigError as exc:
        raise ServeConfigError(str(exc)) from None

    insecure = env_flag(INSECURE_ENV)
    warnings: list[str] = []
    auth: AuthConfig | None = None
    if password_hash is not None:
        auth = AuthConfig(
            password_hash=password_hash,
            session_idle_seconds=_idle_minutes() * 60,
            force_secure_cookie=env_flag(COOKIE_SECURE_ENV),
        )
        if insecure:
            warnings.append(f"{INSECURE_ENV} is ignored because a password is set.")
    elif not loopback:
        if not insecure:
            raise ServeConfigError(
                f"refusing to listen on {bind}: that is reachable from other machines and no "
                "password is set. Set PFA_PASSWORD_FILE (a Docker secret) or PFA_PASSWORD (at "
                "least 8 characters; `pfa auth hash-password` makes a PFA_PASSWORD_HASH that "
                f"keeps the plaintext off disk), or set {INSECURE_ENV}=1 to run without a "
                "login on a network you fully trust."
            )
        warnings.append(
            f"{INSECURE_ENV}=1: listening on {bind} with NO LOGIN. Anyone who can reach this port "
            "can read and change all of your financial data. Set a password instead."
        )

    allowed_hosts = _parse_allowed_hosts(os.environ.get(ALLOWED_HOSTS_ENV, ""))
    if "*" in allowed_hosts and auth is None:
        raise ServeConfigError(f"{ALLOWED_HOSTS_ENV}=* is only allowed together with a password")
    if not loopback and not allowed_hosts:
        warnings.append(
            f"{ALLOWED_HOSTS_ENV} is not set, so only localhost and 127.0.0.1 are accepted as the "
            "address in the browser's URL. Set it to the names you will use, for example "
            f"{ALLOWED_HOSTS_ENV}=nas.local,192.168.1.10 (or * behind the login)."
        )

    return ServeConfig(
        host=bind,
        auth=auth,
        allowed_hosts=allowed_hosts,
        trusted_proxies=os.environ.get(TRUSTED_PROXIES_ENV, "").strip() or None,
        warnings=tuple(warnings),
    )
