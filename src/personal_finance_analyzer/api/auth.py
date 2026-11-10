"""Single-password login for a server that listens beyond loopback (a NAS, a home server).

The localhost design (`api/security.py`) has no users: the per-launch token only proves a request
came from a page this server itself served, and anyone who can reach the port can fetch that page.
That is fine on `127.0.0.1`; on a LAN it is not. When `pfa serve` binds a non-loopback address it
refuses to start without a password (or an explicit ``PFA_INSECURE_NO_AUTH=1``), and this module
puts a login in front of everything:

- **One password**, from ``PFA_PASSWORD`` / ``PFA_PASSWORD_FILE`` (hashed in memory at startup with
  scrypt; the plaintext is not kept) or a pre-computed hash in ``PFA_PASSWORD_HASH`` /
  ``PFA_PASSWORD_HASH_FILE`` (``pfa auth hash-password``), so the plaintext never needs to be
  stored at all. A password set on loopback turns the login on there too.
- **Sessions** are random 256-bit ids kept in memory, carried in an ``HttpOnly``,
  ``SameSite=Strict`` cookie (``Secure`` whenever the request arrived over HTTPS, as reported by a
  trusted reverse proxy, or when ``PFA_COOKIE_SECURE=1``). They expire after an idle period and
  after a fixed maximum age, die on logout, and do not survive a restart (the per-launch token
  changes then anyway).
- **Rate limiting**: an attempt is counted as a failure *before* its password is checked (and
  un-counted if it turns out right), so a burst of parallel requests cannot all slip under the
  limit; past the per-client limit every attempt, right or wrong, gets 429 until the window
  passes. Once the global limit is reached, a client with no failures of its own still gets one
  verified try per window, so a correct password gets through (a few LAN devices cannot lock the
  owner out) while a guesser gets one guess per address, not unlimited ones. Behind a Docker
  port mapping that hides client addresses (`api/clients.py`) all clients share one bucket, so
  5 wrong guesses lock everyone out for the window: the limit still holds, but it cannot tell the
  owner from a stranger there. At most `AuthConfig.max_concurrent_verifications` scrypt
  verifications run at once (each uses ~32 MiB); other requests wait briefly, then get 429.
- **Everything else is locked**: the SPA, the static files and the whole API answer 401 (API and
  non-GET requests) or a redirect to ``/login`` (page loads) without a valid session. Only
  ``/login`` and the health probe are open. The existing Host allowlist and per-launch CSRF token
  keep applying underneath; the login form carries the token too, and a login or logout POST must
  be same-origin.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import html
import ipaddress
import secrets
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from personal_finance_analyzer.api.security import (
    HEALTH_PATH,
    TOKEN_HEADER,
    HostPolicy,
    token_matches,
)
from personal_finance_analyzer.core.container import SecretFileError, read_env_or_file
from personal_finance_analyzer.core.errors import PfaError

SESSION_COOKIE = "pfa_session"
LOGIN_PATH = "/login"
LOGOUT_PATH = "/logout"

PASSWORD_ENV = "PFA_PASSWORD"
PASSWORD_HASH_ENV = "PFA_PASSWORD_HASH"
MIN_PASSWORD_LENGTH = 8

# scrypt: N=2**15, r=8, p=1 uses ~32 MiB and ~100 ms on a desktop CPU; it only runs on a login,
# which the throttle bounds.
_SCRYPT_LOG_N = 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 128 * 1024 * 1024
_SCRYPT_MAX_LOG_N = 20
_HASH_PREFIX = "scrypt"


class AuthConfigError(PfaError):
    """The login password or its settings are missing or invalid. The message is user-facing."""


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """``scrypt$<log2 N>$<r>$<p>$<salt b64>$<hash b64>`` for ``password``. Salted randomly unless
    ``salt`` is given (tests)."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=1 << _SCRYPT_LOG_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=32,
    )
    return "$".join(
        (
            _HASH_PREFIX,
            str(_SCRYPT_LOG_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def parse_password_hash(encoded: str) -> tuple[int, int, int, bytes, bytes]:
    """The ``(log2 N, r, p, salt, digest)`` of an encoded hash.

    Raises:
        ValueError: not a hash produced by `hash_password`, or with absurd cost parameters.
    """
    parts = encoded.strip().split("$")
    if len(parts) != 6 or parts[0] != _HASH_PREFIX:
        raise ValueError("not a scrypt password hash")
    try:
        log_n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4], validate=True)
        digest = base64.b64decode(parts[5], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("malformed password hash") from exc
    if not (1 <= log_n <= _SCRYPT_MAX_LOG_N and 1 <= r <= 32 and 1 <= p <= 16):
        raise ValueError("password hash has out-of-range cost parameters")
    if not salt or not digest:
        raise ValueError("malformed password hash")
    return log_n, r, p, salt, digest


def verify_password(password: str, encoded: str) -> bool:
    """Whether ``password`` matches ``encoded`` (constant-time comparison); False for a malformed
    hash rather than an exception."""
    try:
        log_n, r, p, salt, digest = parse_password_hash(encoded)
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=1 << log_n,
            r=r,
            p=p,
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(digest),
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(candidate, digest)


def load_password_hash() -> str | None:
    """The password verifier configured through the environment, or None when no password is set.

    A plain ``PFA_PASSWORD`` (or ``PFA_PASSWORD_FILE``) is hashed here; a ``PFA_PASSWORD_HASH``
    (or ``_FILE``) is used as given. Setting both kinds is an error.

    Raises:
        AuthConfigError: both a password and a hash are set, the hash is malformed, the password
            is shorter than `MIN_PASSWORD_LENGTH`, or a ``_FILE`` cannot be read.
    """
    try:
        plain = read_env_or_file(PASSWORD_ENV)
        pre_hashed = read_env_or_file(PASSWORD_HASH_ENV)
    except SecretFileError as exc:
        raise AuthConfigError(str(exc)) from None
    if plain and pre_hashed:
        raise AuthConfigError(f"set {PASSWORD_ENV} or {PASSWORD_HASH_ENV}, not both")
    if pre_hashed:
        encoded = pre_hashed.strip()
        try:
            parse_password_hash(encoded)
        except ValueError as exc:
            raise AuthConfigError(
                f"{PASSWORD_HASH_ENV} is not a valid hash ({exc}); create one with "
                "`pfa auth hash-password`"
            ) from None
        return encoded
    if plain:
        if len(plain) < MIN_PASSWORD_LENGTH:
            raise AuthConfigError(
                f"{PASSWORD_ENV} must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        return hash_password(plain)
    return None


@dataclass(frozen=True)
class AuthConfig:
    """Everything the login needs. ``password_hash`` is a `hash_password` string."""

    password_hash: str
    #: A session ends after this long without a request...
    session_idle_seconds: int = 8 * 3600
    #: ...and after this long regardless.
    session_max_seconds: int = 7 * 24 * 3600
    #: Mark the cookie ``Secure`` even when the request does not look like HTTPS.
    force_secure_cookie: bool = False
    #: Failed logins allowed per client address, and in total, per `window_seconds`.
    max_failures_per_client: int = 5
    max_failures_global: int = 40
    #: Hard ceiling on attempts (any client, any outcome but success) per window, as a multiple of
    #: ``max_failures_global``: beyond it every attempt is refused, even a correct password.
    global_ceiling_factor: int = 5
    window_seconds: int = 300
    #: scrypt verifications that may run at once (each holds ~32 MiB), and how long another login
    #: request waits for a free slot before it is answered 429.
    max_concurrent_verifications: int = 2
    verify_wait_seconds: float = 5.0
    #: Monotonic seconds; tests substitute a fake to step through expiry and lockout windows.
    clock: Callable[[], float] = time.monotonic


class SessionStore:
    """In-memory sessions: random id -> (created, last seen). Thread-safe."""

    #: Upper bound on live sessions; the oldest is dropped beyond it.
    MAX_SESSIONS = 256

    def __init__(
        self, idle_seconds: int, max_seconds: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._idle = idle_seconds
        self._max = max_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[float, float]] = {}

    def create(self) -> str:
        sid = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._purge(now)
            while len(self._sessions) >= self.MAX_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)))
            self._sessions[sid] = (now, now)
        return sid

    def valid(self, sid: str | None) -> bool:
        """Whether ``sid`` is a live session; a hit renews its idle timer."""
        if not sid:
            return False
        now = self._clock()
        with self._lock:
            entry = self._sessions.get(sid)
            if entry is None:
                return False
            created, last_seen = entry
            if now - last_seen > self._idle or now - created > self._max:
                del self._sessions[sid]
                return False
            self._sessions[sid] = (created, now)
            return True

    def revoke(self, sid: str | None) -> None:
        if sid:
            with self._lock:
                self._sessions.pop(sid, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _purge(self, now: float) -> None:
        dead = [
            sid
            for sid, (created, last_seen) in self._sessions.items()
            if now - last_seen > self._idle or now - created > self._max
        ]
        for sid in dead:
            del self._sessions[sid]


@dataclass(frozen=True)
class Attempt:
    """A login attempt the throttle has seen. ``wait`` > 0 means it is refused for that many
    seconds; otherwise it is already counted as a failure until `LoginThrottle.succeed` or
    `LoginThrottle.cancel` says otherwise."""

    client: str
    wait: int = 0
    stamp: float = 0.0

    @property
    def allowed(self) -> bool:
        return self.wait == 0


class LoginThrottle:
    """Counts failed logins in a sliding window, per client address and overall. Thread-safe.

    `reserve` checks the limits and, in the same step, records the attempt as a failure, so
    concurrent attempts cannot all pass a check that none of them has charged yet; `succeed`
    takes the charge back when the password was right. Once the global limit is reached each
    client is allowed one attempt per window (instead of ``max_per_client``), so a correct
    password still gets through while a distributed guesser is held to one guess per client
    bucket (see `client_address`). Because clients can still be many, a hard ceiling
    (``max_ceiling`` attempts per window across all clients) refuses every attempt, whoever
    sends it, so total guesses per window are bounded no matter how many addresses are used.
    """

    #: Sweep clients with no failures left in the window when the table grows beyond this.
    _SWEEP_AT = 1024

    def __init__(
        self,
        max_per_client: int,
        max_global: int,
        window_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        max_ceiling: int | None = None,
    ) -> None:
        self._max_client = max_per_client
        self._max_global = max_global
        self._max_ceiling = max_ceiling if max_ceiling is not None else 5 * max_global
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._by_client: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def _trim(self, queue: deque[float], now: float) -> None:
        while queue and now - queue[0] >= self._window:
            queue.popleft()

    def _sweep(self, now: float) -> None:
        for client in list(self._by_client):
            queue = self._by_client[client]
            self._trim(queue, now)
            if not queue:
                del self._by_client[client]

    def _wait(self, client: str, now: float) -> int:
        """Seconds until ``client`` may try again; 0 when it may try now. Lock held by the caller."""
        self._trim(self._all, now)
        if len(self._all) >= self._max_ceiling:
            # Hard ceiling: nobody gets through until the oldest charged attempt leaves the window.
            return max(1, int(self._all[0] + self._window - now) + 1)
        limit = self._max_client
        if len(self._all) >= self._max_global:
            limit = min(limit, 1)
        queue = self._by_client.get(client)
        if queue is not None:
            self._trim(queue, now)
            if not queue:
                del self._by_client[client]
            elif len(queue) >= limit:
                return max(1, int(queue[0] + self._window - now) + 1)
        return 0

    def reserve(self, client: str) -> Attempt:
        """Check the limits and, when the attempt may go ahead, count it as a failure."""
        now = self._clock()
        with self._lock:
            wait = self._wait(client, now)
            if wait:
                return Attempt(client, wait=wait)
            if len(self._by_client) > self._SWEEP_AT:
                self._sweep(now)
            self._all.append(now)
            self._by_client.setdefault(client, deque()).append(now)
            return Attempt(client, stamp=now)

    def succeed(self, attempt: Attempt) -> None:
        """The password was right: take this attempt's charge back and forget the client's
        earlier failures."""
        with self._lock:
            self._refund(attempt)
            self._by_client.pop(attempt.client, None)

    def cancel(self, attempt: Attempt) -> None:
        """The attempt was never checked (no verification slot): take its charge back."""
        with self._lock:
            self._refund(attempt)

    def _refund(self, attempt: Attempt) -> None:
        # Entries are a queue of stamps; remove one equal stamp from each (any equal one will do).
        with contextlib.suppress(ValueError):
            self._all.remove(attempt.stamp)
        queue = self._by_client.get(attempt.client)
        if queue is not None:
            with contextlib.suppress(ValueError):
                queue.remove(attempt.stamp)
            if not queue:
                del self._by_client[attempt.client]


class AuthState:
    """What the middleware and the login routes share (kept on ``app.state.auth``)."""

    def __init__(self, config: AuthConfig) -> None:
        self.config = config
        self.sessions = SessionStore(
            config.session_idle_seconds, config.session_max_seconds, config.clock
        )
        self.throttle = LoginThrottle(
            config.max_failures_per_client,
            config.max_failures_global,
            config.window_seconds,
            config.clock,
            config.max_failures_global * max(1, config.global_ceiling_factor),
        )
        self._slots: asyncio.Semaphore | None = None
        self._slots_loop: asyncio.AbstractEventLoop | None = None

    def verify_slots(self) -> asyncio.Semaphore:
        """The semaphore bounding concurrent password verifications (one per running event loop,
        so the state can outlive a loop, as the test client's does)."""
        loop = asyncio.get_running_loop()
        if self._slots is None or self._slots_loop is not loop:
            self._slots = asyncio.Semaphore(max(1, self.config.max_concurrent_verifications))
            self._slots_loop = loop
        return self._slots


def client_bucket(host: str) -> str:
    """The throttle bucket for a client address: an IPv4 address (also when written as an
    IPv4-mapped IPv6 address) by itself, an IPv6 address by its /64, since one host can use any
    address in its /64 and would otherwise get a fresh set of tries for each."""
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def client_address(request: Request) -> str:
    """The client's throttle bucket: its address as `api/clients.py` resolved it (behind a
    trusted reverse proxy, the forwarded address rather than the proxy's own), normalised by
    `client_bucket`."""
    return client_bucket(request.client.host) if request.client else "unknown"


def safe_next(value: str | None) -> str:
    """A same-site path to return to after login: anything else becomes ``/``."""
    if (
        not value
        or not value.startswith("/")
        or value.startswith(("//", LOGIN_PATH, LOGOUT_PATH))
        or "\\" in value
        or any(ord(ch) < 32 for ch in value)
        or len(value) > 512
    ):
        return "/"
    return value


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "SAMEORIGIN",
}


class AuthMiddleware(BaseHTTPMiddleware):
    """Applies the Host allowlist to every path and requires a live session for all but the login
    page and the health probe. Sits outside `LocalhostGuardMiddleware`, so an anonymous request is
    turned away before the token check is even reached."""

    def __init__(self, app: object, *, state: AuthState, policy: HostPolicy) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._state = state
        self._policy = policy

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path == HEALTH_PATH:
            return await call_next(request)
        if not self._policy.allows(request.headers.get("host", "")):
            return JSONResponse({"detail": "Host not allowed"}, status_code=403)

        if path != LOGIN_PATH and not self._state.sessions.valid(
            request.cookies.get(SESSION_COOKIE)
        ):
            if path.startswith("/api") or request.method != "GET":
                response: Response = JSONResponse(
                    {"detail": "authentication required"}, status_code=401
                )
            else:
                target = path + (f"?{request.url.query}" if request.url.query else "")
                response = RedirectResponse(
                    f"{LOGIN_PATH}?next={quote(safe_next(target), safe='')}", status_code=303
                )
        else:
            response = await call_next(request)

        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if path == LOGIN_PATH or path.startswith("/api") or response.status_code in (303, 401):
            response.headers["Cache-Control"] = "no-store"
        return response


_LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in - Personal Finance Analyzer</title>
<style>
:root {{ color-scheme: light dark; --bg: #f8fafc; --fg: #0f172a; --card: #ffffff;
  --muted: #475569; --border: #cbd5e1; --accent: #1d4ed8; --error: #b91c1c; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #0b1120; --fg: #e2e8f0; --card: #111a2e;
  --muted: #94a3b8; --border: #334155; --accent: #60a5fa; --error: #f87171; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg);
  color: var(--fg); font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
main {{ width: min(24rem, calc(100vw - 2rem)); background: var(--card); padding: 1.5rem;
  border: 1px solid var(--border); border-radius: 0.75rem; }}
h1 {{ font-size: 1.125rem; margin: 0 0 0.25rem; }}
p {{ margin: 0 0 1rem; color: var(--muted); font-size: 0.875rem; }}
label {{ display: block; font-size: 0.875rem; font-weight: 600; margin-bottom: 0.25rem; }}
input[type=password] {{ width: 100%; padding: 0.55rem 0.7rem; font: inherit; color: inherit;
  background: transparent; border: 1px solid var(--border); border-radius: 0.5rem; }}
input[type=password]:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
button {{ margin-top: 1rem; width: 100%; padding: 0.6rem; font: inherit; font-weight: 600;
  color: #fff; background: var(--accent); border: 0; border-radius: 0.5rem; cursor: pointer; }}
@media (prefers-color-scheme: dark) {{ button {{ color: #0b1120; }} }}
.error {{ color: var(--error); font-size: 0.875rem; margin: 0 0 0.75rem; }}
</style>
</head>
<body>
<main>
<h1>Personal Finance Analyzer</h1>
<p>Enter the password to continue.</p>
{error}
<form method="post" action="/login">
<input type="hidden" name="token" value="{token}">
<input type="hidden" name="next" value="{next}">
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Sign in</button>
</form>
</main>
</body>
</html>
"""


def _login_page(token: str, next_path: str, *, error: str = "", status_code: int = 200) -> Response:
    error_html = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    page = _LOGIN_PAGE.format(
        error=error_html,
        token=html.escape(token, quote=True),
        next=html.escape(next_path, quote=True),
    )
    return HTMLResponse(page, status_code=status_code)


def _same_origin(request: Request) -> bool:
    """A browser sends ``Origin`` on every cross-site POST; when present it must name this host."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = request.headers.get("host", "").strip().lower()
    netloc = origin.split("://", 1)[-1].strip().lower()
    return bool(host) and netloc == host


def _set_session_cookie(response: Response, request: Request, state: AuthState, sid: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        httponly=True,
        samesite="strict",
        secure=state.config.force_secure_cookie or request.url.scheme == "https",
        path="/",
    )


def register_auth_routes(app: FastAPI, state: AuthState) -> None:
    """Add ``GET/POST /login`` and ``POST /logout``. Call before the frontend's catch-all route."""

    @app.get(LOGIN_PATH, include_in_schema=False)
    async def login_form(request: Request) -> Response:
        next_path = safe_next(request.query_params.get("next"))
        if state.sessions.valid(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse(next_path, status_code=303)
        return _login_page(app.state.token, next_path)

    @app.post(LOGIN_PATH, include_in_schema=False)
    async def login_submit(request: Request) -> Response:
        if not _same_origin(request):
            return JSONResponse({"detail": "cross-origin login refused"}, status_code=403)
        form = await request.form()
        token = str(form.get("token") or "")
        password = str(form.get("password") or "")
        next_path = safe_next(str(form.get("next") or ""))
        if not token_matches(token, app.state.token):
            # A stale page (the server restarted since it was served): show a fresh form.
            return _login_page(
                app.state.token,
                next_path,
                error="The page expired. Please try again.",
                status_code=403,
            )
        client = client_address(request)
        # Charged as a failure now, before the (slow) check, so parallel requests cannot all pass.
        attempt = state.throttle.reserve(client)
        if not attempt.allowed:
            response = _login_page(
                app.state.token,
                next_path,
                error=f"Too many failed attempts. Try again in {attempt.wait} seconds.",
                status_code=429,
            )
            response.headers["Retry-After"] = str(attempt.wait)
            return response
        slots = state.verify_slots()
        try:
            await asyncio.wait_for(slots.acquire(), state.config.verify_wait_seconds)
        except TimeoutError:
            state.throttle.cancel(attempt)
            response = _login_page(
                app.state.token,
                next_path,
                error="The server is busy checking other sign-ins. Try again in a few seconds.",
                status_code=429,
            )
            response.headers["Retry-After"] = "5"
            return response
        try:
            ok = await run_in_threadpool(verify_password, password, state.config.password_hash)
        finally:
            slots.release()
        if not ok:
            return _login_page(
                app.state.token, next_path, error="Incorrect password.", status_code=401
            )
        state.throttle.succeed(attempt)
        response = RedirectResponse(next_path, status_code=303)
        _set_session_cookie(response, request, state, state.sessions.create())
        return response

    @app.post(LOGOUT_PATH, include_in_schema=False)
    async def logout(request: Request) -> Response:
        supplied = request.headers.get(TOKEN_HEADER, "")
        if not _same_origin(request) or not token_matches(supplied, app.state.token):
            return JSONResponse({"detail": "missing or invalid X-PFA-Token"}, status_code=403)
        state.sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"status": "signed out"})
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
        return response
