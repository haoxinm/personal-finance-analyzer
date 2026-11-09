"""Who may connect: the network-level gate in front of the login (LAN-only by default).

A server that listens beyond loopback (`pfa serve --host 0.0.0.0`, the Docker image) is meant for
the home network. This module makes the application itself refuse everyone else, instead of
trusting that the router never forwards the port:

- **`ClientPolicy`**: the set of networks a *client* address must fall in. The default is the
  private ranges (loopback, RFC 1918, link-local, IPv6 unique-local, and IPv4-mapped forms of
  those). ``PFA_ALLOWED_CLIENTS`` replaces it (``private`` is a keyword for the default set, so
  ``private,100.64.0.0/10`` adds Tailscale's CGNAT range); ``PFA_ALLOW_PUBLIC_CLIENTS=1`` switches
  the check off with a loud warning (see `api/serve_config.py`).
- **`resolve_client`**: the one place that decides what the client address *is*. It is the TCP
  peer, unless the peer is one of ``PFA_TRUSTED_PROXIES``; then it is the first address in
  ``X-Forwarded-For`` that is not itself a trusted proxy, walking right to left (each proxy appends
  the address it saw, so the right-hand entries are the ones our own proxies vouch for; anything
  further left is whatever the client chose to send). A trusted proxy that sends a missing or
  unreadable header yields no address at all, and the request is refused (fail closed).
- **`ClientGuardMiddleware`**: the outermost layer. It applies `resolve_client`, rewrites the
  request's client address (and scheme, from ``X-Forwarded-Proto``) so the login's failed-attempt
  limits count real clients, and answers 403 to a client outside the policy before anything else
  runs, the login page included. It replaces uvicorn's own proxy-header handling, which cannot fail
  closed and, for ``*``, believes the left-most (client-controlled) entry.

This is defense in depth, not the guarantee. The address the app sees is whatever the network
stack hands it. Docker's port publishing often replaces it: IPv6 connections to a published port
are proxied by docker-proxy over IPv4 and arrive from the bridge gateway (172.x), and Docker
Desktop and rootless Docker make every client one private address. Every client then looks
"local" and no IP check can tell them apart. The guarantee is not exposing the port (publish it on
a LAN address or IPv4 only, never forward it on the router; docs/DOCKER.md). `default_gateway`
and `BlockLog.warn_gateway_peer` exist so the app can say so in the log when it sees this.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

#: Keyword for the default set inside ``PFA_ALLOWED_CLIENTS``.
PRIVATE_KEYWORD: Final = "private"

#: Loopback, RFC 1918, link-local and IPv6 unique-local. Deliberately explicit rather than
#: ``ipaddress``'s ``is_private``, which also covers documentation, benchmarking and reserved
#: blocks, and deliberately without 100.64.0.0/10 (carrier-grade NAT, used by Tailscale): that
#: range is shared with strangers on some ISPs, so it is opt-in.
DEFAULT_NETWORKS: Final[tuple[IPNetwork, ...]] = tuple(
    ipaddress.ip_network(text)
    for text in (
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "fe80::/10",
        "fc00::/7",
    )
)

#: Refused in ``PFA_ALLOWED_CLIENTS``: a network this wide is "everyone", which has its own,
#: deliberately noisy switch (``PFA_ALLOW_PUBLIC_CLIENTS``).
_MIN_PREFIX_V4: Final = 8
_MIN_PREFIX_V6: Final = 7

_log = logging.getLogger("personal_finance_analyzer.api.clients")


ROUTE_TABLE: Final = Path("/proc/net/route")

_RTF_GATEWAY: Final = 0x2


def default_gateway(route_table: str | None = None) -> str | None:
    """The IPv4 address of this host's (container's) default gateway, or None when unknown.

    Read from ``/proc/net/route`` (Linux) unless ``route_table`` is given (tests). The table has
    one route per line after a header; the default route has destination ``00000000`` and the
    ``RTF_GATEWAY`` flag, and the gateway is a little-endian hex word.
    """
    if route_table is None:
        try:
            route_table = ROUTE_TABLE.read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
            gateway = int(fields[2], 16)
        except ValueError:
            continue
        if not flags & _RTF_GATEWAY or gateway == 0:
            continue
        return str(ipaddress.IPv4Address(gateway.to_bytes(4, "little")))
    return None


def parse_ip(text: str | None) -> IPAddress | None:
    """The address in ``text``, or None when it is not one.

    Accepts what a peer address or an ``X-Forwarded-For`` entry can look like: a bare IPv4 or
    IPv6 address, ``1.2.3.4:5678``, ``[::1]``, ``[::1]:5678`` and a ``%zone`` suffix. An
    IPv4-mapped IPv6 address (``::ffff:192.168.1.5``) becomes the IPv4 address it wraps, so every
    comparison sees one form.
    """
    if text is None:
        return None
    value = text.strip()
    if not value:
        return None
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        rest = value[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
        value = value[1:end]
    elif value.count(":") == 1:  # host:port; a bare IPv6 address has several colons
        value, _, port = value.partition(":")
        if not port.isdigit():
            return None
    value = value.partition("%")[0]
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


class TrustedProxies:
    """``PFA_TRUSTED_PROXIES``: addresses or CIDRs whose forwarding headers are believed, or
    ``*`` for every sender (only sensible when the port is reachable by the proxy alone)."""

    def __init__(self, networks: Iterable[IPNetwork] = (), *, any_sender: bool = False) -> None:
        self.networks: tuple[IPNetwork, ...] = tuple(networks)
        self.any_sender = any_sender

    @classmethod
    def parse(cls, raw: str) -> TrustedProxies:
        """Parse the comma-separated setting.

        Raises:
            ValueError: an entry is neither an address, a CIDR nor ``*``, or nothing is listed.
        """
        networks: list[IPNetwork] = []
        any_sender = False
        for part in raw.split(","):
            entry = part.strip()
            if not entry:
                continue
            if entry == "*":
                any_sender = True
                continue
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                raise ValueError(
                    f"{entry!r} is not an IP address or CIDR range such as 172.18.0.0/16 (or *)"
                ) from None
        if not networks and not any_sender:
            raise ValueError("no proxy address given")
        return cls(networks, any_sender=any_sender)

    def __contains__(self, address: IPAddress) -> bool:
        if self.any_sender:
            return True
        return any(address in network for network in self.networks)

    def describe(self) -> str:
        if self.any_sender:
            return "any sender (*)"
        return ", ".join(str(network) for network in self.networks)


@dataclass(frozen=True)
class ResolvedClient:
    """The outcome of `resolve_client`."""

    #: The client's address; None when it cannot be determined (the request must be refused).
    address: str | None
    #: The TCP peer, when it is a readable address.
    peer: str | None
    #: Whether the peer is a trusted proxy (so its forwarding headers were consulted).
    via_proxy: bool


def resolve_client(
    peer: str | None, forwarded_for: str | None, trusted: TrustedProxies | None
) -> ResolvedClient:
    """The one definition of "which client sent this request".

    ``peer`` is the TCP peer's address and ``forwarded_for`` the (joined) ``X-Forwarded-For``
    value. The header is read only when ``peer`` is a trusted proxy; from anyone else it is
    ignored, because anyone can write it. From a trusted proxy the entries are walked right to
    left and the first one that is not itself a trusted proxy is the client. If every entry is a
    trusted proxy, the left-most is. A missing, empty or unreadable header (at the point the walk
    reaches it) leaves ``address`` None, and so does an unreadable peer.
    """
    peer_ip = parse_ip(peer)
    if peer_ip is None:
        return ResolvedClient(None, None, False)
    peer_text = str(peer_ip)
    if trusted is None or peer_ip not in trusted:
        return ResolvedClient(peer_text, peer_text, False)
    if not forwarded_for or not forwarded_for.strip():
        return ResolvedClient(None, peer_text, True)
    hops = [parse_ip(hop) for hop in forwarded_for.split(",")]
    for hop in reversed(hops):
        if hop is None:
            return ResolvedClient(None, peer_text, True)
        if trusted.any_sender or hop not in trusted:
            return ResolvedClient(str(hop), peer_text, True)
    leftmost = hops[0]
    return ResolvedClient(str(leftmost) if leftmost else None, peer_text, True)


@dataclass(frozen=True)
class ClientPolicy:
    """The networks a client address must belong to."""

    networks: tuple[IPNetwork, ...] = DEFAULT_NETWORKS

    def allows(self, address: str | None) -> bool:
        ip = parse_ip(address)
        return ip is not None and any(ip in network for network in self.networks)

    def describe(self) -> str:
        if self.networks == DEFAULT_NETWORKS:
            return f"{PRIVATE_KEYWORD} (" + ", ".join(str(n) for n in self.networks) + ")"
        return ", ".join(str(network) for network in self.networks)

    @classmethod
    def parse(cls, raw: str | None) -> ClientPolicy:
        """Parse ``PFA_ALLOWED_CLIENTS``: comma-separated CIDRs or addresses, and ``private``
        for the default set. Empty or unset means the default set.

        Raises:
            ValueError: an entry is invalid, or so wide that it means "everyone".
        """
        entries = [part.strip() for part in (raw or "").split(",") if part.strip()]
        if not entries:
            return cls()
        networks: list[IPNetwork] = []
        for entry in entries:
            if entry.lower() == PRIVATE_KEYWORD:
                found: tuple[IPNetwork, ...] = DEFAULT_NETWORKS
            else:
                try:
                    network = ipaddress.ip_network(entry, strict=False)
                except ValueError:
                    raise ValueError(
                        f"{entry!r} is not an IP address or CIDR range such as 192.168.1.0/24 "
                        f"(or the keyword {PRIVATE_KEYWORD!r})"
                    ) from None
                minimum = _MIN_PREFIX_V4 if network.version == 4 else _MIN_PREFIX_V6
                if network.prefixlen < minimum:
                    raise ValueError(
                        f"{entry!r} is far too wide: it would let (almost) the whole internet in. "
                        "List your own network, or set PFA_ALLOW_PUBLIC_CLIENTS=1 if you really "
                        "mean to accept everyone."
                    )
                found = (network,)
            for network in found:
                if network not in networks:
                    networks.append(network)
        return cls(tuple(networks))


class BlockLog:
    """Rate-limited log lines for refused requests: the first block from an address is logged,
    the same address again only after ``repeat_seconds``, and never more than ``max_per_minute``
    lines in total (the rest are counted and reported in the next line that does get through)."""

    def __init__(
        self,
        *,
        allowed: str,
        clock: Callable[[], float] = time.monotonic,
        repeat_seconds: float = 3600.0,
        max_per_minute: int = 6,
        max_tracked: int = 512,
    ) -> None:
        self._allowed = allowed
        self._clock = clock
        self._repeat = repeat_seconds
        self._max_per_minute = max_per_minute
        self._max_tracked = max_tracked
        self._lock = threading.Lock()
        self._last_logged: dict[str, float] = {}
        self._recent: list[float] = []
        self._suppressed = 0
        self._hinted = False
        self._gateway_warned = False

    def blocked(self, resolved: ResolvedClient) -> None:
        """Record a refusal (``resolved.address`` is None when it could not be determined)."""
        if resolved.address is None:
            if resolved.via_proxy:
                key = f"proxy:{resolved.peer}"
                what = (
                    f"a request through the trusted proxy {resolved.peer} with a missing or "
                    "unreadable X-Forwarded-For header"
                )
            else:
                key = "unreadable"
                what = "a request whose client address could not be determined"
        else:
            key = resolved.address
            what = f"a request from {resolved.address}"
        now = self._clock()
        with self._lock:
            last = self._last_logged.get(key)
            if last is not None and now - last < self._repeat:
                return
            self._recent = [t for t in self._recent if now - t < 60.0]
            if len(self._recent) >= self._max_per_minute:
                self._suppressed += 1
                return
            if len(self._last_logged) >= self._max_tracked:
                self._last_logged.clear()
            self._last_logged[key] = now
            self._recent.append(now)
            suppressed, self._suppressed = self._suppressed, 0
        extra = f" ({suppressed} more refused since the last line)" if suppressed else ""
        _log.warning(
            "Refused %s: only these client networks are allowed: %s. Repeats from the same "
            "address are not logged again for an hour.%s",
            what,
            self._allowed,
            extra,
        )

    def hint_untrusted_forwarding(self, peer: str) -> None:
        """Once per process: a request from a not-trusted peer carried ``X-Forwarded-For``."""
        with self._lock:
            if self._hinted:
                return
            self._hinted = True
        _log.warning(
            "A request from %s carried an X-Forwarded-For header, which is ignored because %s is "
            "not in PFA_TRUSTED_PROXIES. If %s is your reverse proxy, add it there; until then "
            "every visitor it forwards looks like %s to the client check.",
            peer,
            peer,
            peer,
            peer,
        )


    def warn_gateway_peer(self, gateway: str) -> None:
        """Once per process: a request arrived from this container's default gateway."""
        with self._lock:
            if self._gateway_warned:
                return
            self._gateway_warned = True
        _log.warning(
            "Requests are arriving from %s, this container's default gateway. With Docker's port "
            "publishing (docker-proxy, IPv6 connections to a published port, Docker Desktop, "
            "rootless Docker) every client can look like this one private address, so the "
            "local-network check and the per-client login limits cannot tell clients apart here. "
            "Do not rely on them: publish the port on your LAN address only (for example "
            "'192.168.1.10:8756:8756') or on IPv4 only ('0.0.0.0:8756:8756'), and never forward "
            "it on your router.",
            gateway,
        )


_FORBIDDEN_BODY = {"detail": "This server only accepts connections from the local network."}


class ClientGuardMiddleware:
    """Pure ASGI, outermost. Resolves the client (`resolve_client`), applies the policy (403 with
    no further processing for a client outside it), and publishes the resolved address and
    scheme to everything inside (``scope["client"]``, ``scope["scheme"]``).

    With ``policy=None`` it only performs the proxy-header rewriting. ``gateway`` is the
    container's default gateway (`default_gateway`): the first request whose peer is exactly that
    address logs a one-time warning that the check cannot distinguish clients in this setup.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        policy: ClientPolicy | None = None,
        trusted: TrustedProxies | None = None,
        block_log: BlockLog | None = None,
        gateway: str | None = None,
    ) -> None:
        self.app = app
        self._policy = policy
        self._trusted = trusted
        self._block_log = block_log
        gateway_ip = parse_ip(gateway)
        self._gateway = str(gateway_ip) if gateway_ip is not None else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        peer_tuple = scope.get("client")
        peer = peer_tuple[0] if peer_tuple else None
        forwarded_for: list[str] = []
        forwarded_proto: str | None = None
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                forwarded_for.append(value.decode("latin1"))
            elif name == b"x-forwarded-proto":
                forwarded_proto = value.decode("latin1").strip().lower()
        resolved = resolve_client(peer, ", ".join(forwarded_for) or None, self._trusted)

        if self._policy is not None:
            if resolved.address is None or not self._policy.allows(resolved.address):
                if self._block_log is not None:
                    self._block_log.blocked(resolved)
                await self._refuse(scope, receive, send)
                return
            if (
                self._gateway is not None
                and self._block_log is not None
                and not resolved.via_proxy
                and resolved.peer == self._gateway
            ):
                self._block_log.warn_gateway_peer(self._gateway)
            if (
                forwarded_for
                and not resolved.via_proxy
                and self._block_log is not None
                and resolved.peer is not None
            ):
                self._block_log.hint_untrusted_forwarding(resolved.peer)

        if resolved.via_proxy:
            if resolved.address is not None:
                scope["client"] = (resolved.address, 0)
            if forwarded_proto in ("http", "https"):
                scope["scheme"] = (
                    forwarded_proto
                    if scope["type"] == "http"
                    else forwarded_proto.replace("http", "ws")
                )
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(
            _FORBIDDEN_BODY, status_code=403, headers={"Cache-Control": "no-store"}
        )
        await response(scope, receive, send)
