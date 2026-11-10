"""Localhost hardening (I8, A31).

Binding `127.0.0.1` does not stop a page open in the user's browser from reaching the API: DNS
rebinding lets a malicious page's script address `127.0.0.1` directly once resolved, and a
same-origin-looking `<form>` POST needs no CORS preflight. Two independent checks close this:

- **Host allowlist.** Every request's `Host` header must be exactly `127.0.0.1:<port>` or
  `localhost:<port>` for the port this server is actually bound to (or the bare hostname, for
  a default-port request); anything else is rejected with 403 before it reaches a route.
- **Per-launch token.** A random token is generated once per `serve` start (`generate_token()`)
  and injected into the served `index.html` (see `api/app.py`). Every `/api` request must carry
  it in `X-PFA-Token`, except the SSE endpoint, which takes it as a `token` query parameter
  because `EventSource` cannot set headers.

Both checks apply only under `/api` — the static frontend files need no token to be *served*
(the browser needs the page before it can read the token out of it), but every *data* route does.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

#: Header the per-launch token must arrive in for an ordinary `/api` request (A31).
TOKEN_HEADER = "X-PFA-Token"

#: Query parameter the per-launch token arrives in for the SSE endpoint, which `EventSource`
#: cannot attach a header to (A31).
TOKEN_QUERY_PARAM = "token"

_ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost")


def generate_token() -> str:
    """Return a fresh, cryptographically random per-launch token (A31)."""
    return secrets.token_urlsafe(32)


class LocalhostGuardMiddleware(BaseHTTPMiddleware):
    """Rejects any `/api` request whose `Host` is not `127.0.0.1`/`localhost` (optionally with
    this server's own port) with 403, and any `/api` request that does not carry the per-launch
    token, in `X-PFA-Token` or (SSE only) the `token` query parameter, with 403."""

    def __init__(self, app: object, *, token: str, port: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._token = token
        self._port = port

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not request.url.path.startswith("/api"):
            return await call_next(request)

        host_header = request.headers.get("host", "")
        hostname, _, port_str = host_header.partition(":")
        port_ok = port_str == "" or port_str == str(self._port)
        if hostname not in _ALLOWED_HOSTNAMES or not port_ok:
            return JSONResponse({"detail": "Host not allowed"}, status_code=403)

        supplied = request.headers.get(TOKEN_HEADER) or request.query_params.get(TOKEN_QUERY_PARAM)
        if not supplied or not secrets.compare_digest(supplied, self._token):
            return JSONResponse({"detail": "missing or invalid X-PFA-Token"}, status_code=403)

        return await call_next(request)
