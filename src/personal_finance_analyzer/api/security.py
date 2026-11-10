"""Localhost hardening (I8, A31).

Binding `127.0.0.1` does not stop a page open in the user's browser from reaching the API: DNS
rebinding lets a malicious page's script address `127.0.0.1` directly once resolved, and a
same-origin-looking `<form>` POST needs no CORS preflight. Two independent checks close this:

- **Host allowlist.** Every request's `Host` header must be exactly `127.0.0.1:<port>` or
  `localhost:<port>` for the port this server is actually bound to (or the bare hostname, for
  a default-port request); anything else is rejected with 403 before it reaches a route. A server
  that is reached by another name (a NAS, a reverse proxy) lists that name with
  ``PFA_ALLOWED_HOSTS``; see `HostPolicy`.
- **Per-launch token.** A random token is generated once per `serve` start (`generate_token()`)
  and injected into the served `index.html` (see `api/app.py`). Every `/api` request must carry
  it in `X-PFA-Token`, except the SSE endpoint, which takes it as a `token` query parameter
  because `EventSource` cannot set headers.

Both checks apply only under `/api` — the static frontend files need no token to be *served*
(the browser needs the page before it can read the token out of it), but every *data* route does.
The one exception is `HEALTH_PATH`, which answers a bare "ok" to anything (a container healthcheck
and a reverse proxy's probe use it).

When the server listens beyond loopback, `api/auth.py` adds a login in front of both.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

#: Header the per-launch token must arrive in for an ordinary `/api` request (A31).
TOKEN_HEADER = "X-PFA-Token"

#: Query parameter the per-launch token arrives in for the SSE endpoint, which `EventSource`
#: cannot attach a header to (A31).
TOKEN_QUERY_PARAM = "token"

#: The unauthenticated liveness probe: needs no token, session or Host match, and reveals nothing.
HEALTH_PATH = "/api/health"

_ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost")


def generate_token() -> str:
    """Return a fresh, cryptographically random per-launch token (A31)."""
    return secrets.token_urlsafe(32)


def token_matches(supplied: str, expected: str) -> bool:
    """Constant-time comparison that also accepts non-ASCII input (``compare_digest`` on two
    ``str`` raises ``TypeError`` for it, which would be a 500 instead of a refusal)."""
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def split_host_header(value: str) -> tuple[str, str]:
    """``(hostname, port)`` of a ``Host`` header value, lower-cased; ``port`` is ``""`` when absent.
    Understands bracketed IPv6 (``[::1]:8756``)."""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, ""
        host = value[: end + 1]
        rest = value[end + 1 :]
        return host, rest[1:] if rest.startswith(":") else ""
    host, _, port = value.partition(":")
    return host, port


class HostPolicy:
    """Which ``Host`` header values are accepted.

    ``127.0.0.1`` and ``localhost`` are always accepted, on the bound port (or none). ``extra``
    adds names: ``nas.local`` accepts any port (a published port can differ from the bound one,
    and a reverse proxy uses the default port), ``nas.local:9000`` only that port, and ``*``
    anything (only sensible behind the login).
    """

    def __init__(self, port: int, extra: Iterable[str] = ()) -> None:
        self._port = str(port)
        self._any = False
        self._any_port: set[str] = set()
        self._exact: set[tuple[str, str]] = set()
        for raw in extra:
            entry = raw.strip().lower()
            if not entry:
                continue
            if entry == "*":
                self._any = True
                continue
            host, entry_port = split_host_header(entry)
            if entry_port:
                self._exact.add((host, entry_port))
            else:
                self._any_port.add(host)

    @property
    def allows_any(self) -> bool:
        return self._any

    def allows(self, host_header: str) -> bool:
        if self._any:
            return True
        host, port = split_host_header(host_header)
        if host in _ALLOWED_HOSTNAMES and (port == "" or port == self._port):
            return True
        return host in self._any_port or (host, port) in self._exact


class LocalhostGuardMiddleware(BaseHTTPMiddleware):
    """Rejects any `/api` request whose `Host` is not accepted by the `HostPolicy` (by default
    `127.0.0.1`/`localhost`, optionally with this server's own port) with 403, and any `/api`
    request that does not carry the per-launch token, in `X-PFA-Token` or (SSE only) the `token`
    query parameter, with 403. `HEALTH_PATH` is exempt from both."""

    def __init__(
        self, app: object, *, token: str, port: int, allowed_hosts: Iterable[str] = ()
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._token = token
        self._policy = HostPolicy(port, allowed_hosts)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if not path.startswith("/api") or path == HEALTH_PATH:
            return await call_next(request)

        if not self._policy.allows(request.headers.get("host", "")):
            return JSONResponse({"detail": "Host not allowed"}, status_code=403)

        supplied = request.headers.get(TOKEN_HEADER) or request.query_params.get(TOKEN_QUERY_PARAM)
        if not supplied or not token_matches(supplied, self._token):
            return JSONResponse({"detail": "missing or invalid X-PFA-Token"}, status_code=403)

        return await call_next(request)
