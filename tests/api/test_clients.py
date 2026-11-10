"""LAN-only: the client-network gate and the one place that decides who the client is
(`api/clients.py`), exercised through the real app with a configurable peer address."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from personal_finance_analyzer.api.app import create_app
from personal_finance_analyzer.api.auth import AuthConfig, hash_password
from personal_finance_analyzer.api.clients import (
    BlockLog,
    ClientGuardMiddleware,
    ClientPolicy,
    ResolvedClient,
    TrustedProxies,
    default_gateway,
    parse_ip,
    resolve_client,
)
from personal_finance_analyzer.config import Settings

PASSWORD = "correct horse battery"
TOKEN = "test-token"
PASSWORD_HASH = hash_password(PASSWORD)
PROXY = "172.18.0.2"  # a reverse proxy container on the Docker network
INDEX = "<html><head><title>x</title></head><body>app</body></html>"

# --- the address matrix --------------------------------------------------------------------

ALLOWED = [
    "127.0.0.1",
    "127.1.2.3",
    "::1",
    "10.0.0.1",
    "10.255.255.254",
    "172.16.0.1",
    "172.31.255.254",
    "192.168.0.1",
    "192.168.1.77",
    "169.254.10.20",
    "fe80::1",
    "fe80::abcd:1%eth0",
    "fc00::1",
    "fd12:3456:789a::1",
    "::ffff:192.168.1.5",
    "::ffff:10.1.2.3",
    "::ffff:127.0.0.1",
    "[fd00::5]:443",
    "192.168.1.9:51234",
]
BLOCKED = [
    "8.8.8.8",
    "1.1.1.1",
    "203.0.113.9",
    "172.15.255.255",  # just below 172.16/12
    "172.32.0.1",  # just above it
    "192.167.1.1",
    "11.0.0.1",
    "100.64.0.1",  # CGNAT: opt-in only
    "100.127.255.254",
    "2001:db8::1",
    "2606:4700:4700::1111",
    "::ffff:8.8.8.8",
    "fe00::1",  # not fe80::/10, not fc00::/7
    "::",
    "0.0.0.0",
    "not-an-ip",
    "",
]


@pytest.mark.parametrize("address", ALLOWED)
def test_default_policy_allows_local_addresses(address: str) -> None:
    assert ClientPolicy().allows(address)


@pytest.mark.parametrize("address", BLOCKED)
def test_default_policy_blocks_everything_else(address: str) -> None:
    assert not ClientPolicy().allows(address)


def test_no_address_is_never_allowed() -> None:
    assert not ClientPolicy().allows(None)


def test_parse_ip_unwraps_mapped_addresses_and_ports() -> None:
    assert str(parse_ip("::ffff:192.168.1.5")) == "192.168.1.5"
    assert str(parse_ip("[::ffff:8.8.8.8]:80")) == "8.8.8.8"
    assert str(parse_ip(" 1.2.3.4:5678 ")) == "1.2.3.4"
    assert str(parse_ip("fe80::1%en0")) == "fe80::1"
    for bad in ("", "  ", "unknown", "1.2.3.4:port", "[::1", "[::1]x", "1.2.3", "1.2.3.4.5", None):
        assert parse_ip(bad) is None


# --- PFA_ALLOWED_CLIENTS parsing -----------------------------------------------------------


def test_allowed_clients_replaces_the_default() -> None:
    policy = ClientPolicy.parse("192.168.1.0/24")
    assert policy.allows("192.168.1.200")
    assert policy.allows("::ffff:192.168.1.200")
    assert not policy.allows("192.168.2.1")
    assert not policy.allows("10.0.0.1")
    assert not policy.allows("127.0.0.1")  # replaced means replaced


def test_private_keyword_is_the_default_set_and_combines() -> None:
    policy = ClientPolicy.parse("private,100.64.0.0/10")
    assert policy.allows("192.168.1.1")
    assert policy.allows("fd00::1")
    assert policy.allows("100.100.100.100")
    assert not policy.allows("8.8.8.8")
    assert ClientPolicy.parse("PRIVATE").networks == ClientPolicy().networks


def test_unset_or_blank_means_the_default() -> None:
    assert ClientPolicy.parse(None).networks == ClientPolicy().networks
    assert ClientPolicy.parse(" , ,").networks == ClientPolicy().networks


def test_single_addresses_and_ipv6_ranges_are_accepted() -> None:
    policy = ClientPolicy.parse("203.0.113.7, 2001:db8:1::/48")
    assert policy.allows("203.0.113.7")
    assert not policy.allows("203.0.113.8")
    assert policy.allows("2001:db8:1::99")
    assert not policy.allows("2001:db8:2::1")


@pytest.mark.parametrize(
    "bad", ["lan", "192.168.1.0/33", "1.2.3", "0.0.0.0/0", "::/0", "8.0.0.0/7"]
)
def test_allowed_clients_rejects_invalid_and_everyone_ranges(bad: str) -> None:
    with pytest.raises(ValueError):
        ClientPolicy.parse(bad)


# --- resolve_client: one definition of "who is the client" ---------------------------------

TRUSTED = TrustedProxies.parse("172.18.0.0/16, 10.9.9.9")


def _client(
    peer: str | None, xff: str | None, trusted: TrustedProxies | None = TRUSTED
) -> str | None:
    return resolve_client(peer, xff, trusted).address


def test_untrusted_peer_is_the_client_and_its_header_is_ignored() -> None:
    assert _client("192.168.1.9", "8.8.8.8") == "192.168.1.9"
    assert _client("8.8.8.8", "192.168.1.5") == "8.8.8.8"
    assert _client("192.168.1.9", "8.8.8.8", None) == "192.168.1.9"
    assert _client("::ffff:192.168.1.9", None) == "192.168.1.9"


def test_trusted_peer_hands_over_to_the_forwarded_address() -> None:
    assert _client(PROXY, "192.168.1.50") == "192.168.1.50"
    assert _client(PROXY, "8.8.8.8") == "8.8.8.8"
    assert _client(PROXY, "[2001:db8::1]:4430") == "2001:db8::1"
    assert _client(PROXY, "::ffff:192.168.1.50") == "192.168.1.50"


def test_forwarded_chain_is_walked_right_to_left_skipping_trusted_proxies() -> None:
    assert _client(PROXY, "8.8.8.8, 172.18.0.5") == "8.8.8.8"
    assert _client(PROXY, "8.8.8.8, 172.18.0.5, 10.9.9.9") == "8.8.8.8"
    # A client that prepends a fake private address does not get to be it:
    assert _client(PROXY, "192.168.1.50, 8.8.8.8") == "8.8.8.8"
    # What our own proxy appended wins over anything to its left:
    assert _client(PROXY, "8.8.8.8, 192.168.1.50") == "192.168.1.50"
    # Several header lines count as one list, in order:
    assert _client(PROXY, "192.168.1.50, 8.8.8.8") == _client(PROXY, "192.168.1.50,8.8.8.8")


def test_all_hops_trusted_means_the_leftmost_is_the_client() -> None:
    assert _client(PROXY, "172.18.0.9, 172.18.0.5") == "172.18.0.9"


@pytest.mark.parametrize("xff", [None, "", "   ", ",", "unknown", "8.8.8.8, garbage", "1.2.3"])
def test_missing_or_unreadable_forwarding_from_a_trusted_proxy_resolves_to_nothing(
    xff: str | None,
) -> None:
    resolved = resolve_client(PROXY, xff, TRUSTED)
    assert resolved.address is None
    assert resolved.via_proxy


def test_unreadable_garbage_left_of_the_deciding_hop_is_never_read() -> None:
    assert _client(PROXY, "garbage, 192.168.1.5") == "192.168.1.5"


def test_unreadable_peer_resolves_to_nothing() -> None:
    assert _client("testclient", None) is None
    assert _client(None, None) is None


def test_wildcard_trust_uses_the_rightmost_entry_not_the_client_controlled_one() -> None:
    star = TrustedProxies.parse("*")
    assert _client("8.8.8.8", "192.168.1.5, 8.8.4.4", star) == "8.8.4.4"


def test_trusted_proxies_parsing() -> None:
    assert TrustedProxies.parse("*").any_sender
    parsed = TrustedProxies.parse(" 172.18.0.0/16 , 10.0.0.5 ,")
    assert parse_ip("172.18.9.9") in parsed  # type: ignore[operator]
    assert parse_ip("10.0.0.5") in parsed  # type: ignore[operator]
    assert parse_ip("10.0.0.6") not in parsed  # type: ignore[operator]
    for bad in ("", " , ", "traefik", "172.18.0.0/99"):
        with pytest.raises(ValueError):
            TrustedProxies.parse(bad)


# --- the middleware, through the real app ---------------------------------------------------


@pytest.fixture
def web_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "web"
    directory.mkdir()
    (directory / "index.html").write_text(INDEX, encoding="utf-8")
    return directory


@pytest.fixture
def make_app(engine: Engine, session: Session, web_dir: Path) -> Callable[..., FastAPI]:
    del session

    def build(
        *,
        policy: ClientPolicy | None = None,
        trusted: TrustedProxies | None = None,
        auth: bool = True,
    ) -> FastAPI:
        return create_app(
            engine=engine,
            settings=Settings(),
            token=TOKEN,
            web_dir=web_dir,
            auth=AuthConfig(password_hash=PASSWORD_HASH) if auth else None,
            allowed_hosts=("nas.local",),
            client_policy=policy,
            trusted_proxies=trusted,
        )

    return build


def _browser(app: FastAPI, peer: str) -> TestClient:
    return TestClient(
        app, base_url="http://nas.local:8756", follow_redirects=False, client=(peer, 40000)
    )


@pytest.fixture
def lan_app(make_app: Callable[..., FastAPI]) -> FastAPI:
    return make_app(policy=ClientPolicy())


@pytest.mark.parametrize(
    "peer", ["192.168.1.50", "10.0.0.8", "127.0.0.1", "::1", "fd00::7", "fe80::2"]
)
def test_lan_peer_sees_the_login_page(lan_app: FastAPI, peer: str) -> None:
    response = _browser(lan_app, peer).get("/login")
    assert response.status_code == 200
    assert "Enter the password" in response.text


def test_ipv4_mapped_lan_peer_is_allowed(lan_app: FastAPI) -> None:
    assert _browser(lan_app, "::ffff:192.168.1.50").get("/login").status_code == 200


@pytest.mark.parametrize(
    "peer", ["8.8.8.8", "203.0.113.9", "100.64.0.1", "2001:db8::1", "::ffff:8.8.8.8"]
)
def test_public_peer_gets_403_before_the_login_page(lan_app: FastAPI, peer: str) -> None:
    browser = _browser(lan_app, peer)
    for method, path in (
        ("get", "/login"),
        ("get", "/"),
        ("get", "/api/health"),
        ("get", "/api/users"),
        ("post", "/login"),
        ("post", "/logout"),
    ):
        response = getattr(browser, method)(path)
        assert response.status_code == 403, (method, path)
        assert "password" not in response.text.lower()
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers


def test_public_peer_cannot_even_try_a_password(lan_app: FastAPI) -> None:
    response = _browser(lan_app, "8.8.8.8").post(
        "/login", data={"password": PASSWORD, "token": TOKEN, "next": "/"}
    )
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_an_unreadable_peer_is_refused(lan_app: FastAPI) -> None:
    assert _browser(lan_app, "testclient").get("/login").status_code == 403


def test_health_from_loopback_works_for_the_container_healthcheck(lan_app: FastAPI) -> None:
    response = _browser(lan_app, "127.0.0.1").get("/api/health", headers={"Host": "127.0.0.1:8756"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_configured_cidrs_replace_the_default(make_app: Callable[..., FastAPI]) -> None:
    app = make_app(policy=ClientPolicy.parse("192.168.1.0/24"))
    assert _browser(app, "192.168.1.5").get("/login").status_code == 200
    assert _browser(app, "192.168.2.5").get("/login").status_code == 403
    assert _browser(app, "10.0.0.5").get("/login").status_code == 403


def test_private_keyword_plus_tailscale(make_app: Callable[..., FastAPI]) -> None:
    app = make_app(policy=ClientPolicy.parse("private,100.64.0.0/10"))
    assert _browser(app, "192.168.1.5").get("/login").status_code == 200
    assert _browser(app, "100.101.102.103").get("/login").status_code == 200
    assert _browser(app, "8.8.8.8").get("/login").status_code == 403


def test_the_check_also_covers_a_server_with_no_login(make_app: Callable[..., FastAPI]) -> None:
    app = make_app(policy=ClientPolicy(), auth=False)
    assert _browser(app, "192.168.1.5").get("/api/health").status_code == 200
    assert _browser(app, "8.8.8.8").get("/api/health").status_code == 403


# --- behind a reverse proxy -----------------------------------------------------------------


@pytest.fixture
def proxied_app(make_app: Callable[..., FastAPI]) -> FastAPI:
    return make_app(policy=ClientPolicy(), trusted=TrustedProxies.parse("172.18.0.0/16"))


def _via(app: FastAPI, peer: str, *forwarded: str, **headers: str) -> int:
    pairs = [("x-forwarded-for", value) for value in forwarded] + [
        (name.replace("_", "-"), value) for name, value in headers.items()
    ]
    return _browser(app, peer).get("/login", headers=pairs).status_code


def test_public_client_through_a_trusted_proxy_is_blocked(proxied_app: FastAPI) -> None:
    assert _via(proxied_app, PROXY, "8.8.8.8") == 403
    assert _via(proxied_app, PROXY, "2001:db8::1") == 403
    assert _via(proxied_app, PROXY, "[2001:db8::1]:443") == 403
    assert _via(proxied_app, PROXY, "8.8.8.8, 172.18.0.5") == 403
    assert _via(proxied_app, PROXY, "192.168.1.50, 8.8.8.8") == 403  # spoofed private prefix


def test_private_client_through_a_trusted_proxy_is_allowed(proxied_app: FastAPI) -> None:
    assert _via(proxied_app, PROXY, "192.168.1.50") == 200
    assert _via(proxied_app, PROXY, "fd00::9") == 200
    assert _via(proxied_app, PROXY, "8.8.8.8, 192.168.1.50") == 200  # our proxy vouches for .50
    assert _via(proxied_app, PROXY, "192.168.1.50, 172.18.0.7") == 200
    assert _via(proxied_app, PROXY, "192.168.1.50", "172.18.0.7") == 200  # two header lines


def test_untrusted_peer_sending_forwarded_for_is_ignored(proxied_app: FastAPI) -> None:
    # A LAN machine that is not a proxy: its header changes nothing, either way.
    assert _via(proxied_app, "192.168.1.9", "8.8.8.8") == 200
    # The internet cannot claim to be a LAN client.
    assert _via(proxied_app, "8.8.8.8", "192.168.1.50") == 403
    assert _via(proxied_app, "8.8.8.8", "127.0.0.1") == 403


@pytest.mark.parametrize("forwarded", [(), ("",), ("unknown",), ("8.8.8.8, nonsense",), (",",)])
def test_missing_or_garbled_forwarding_from_a_trusted_proxy_fails_closed(
    proxied_app: FastAPI, forwarded: tuple[str, ...]
) -> None:
    assert _via(proxied_app, PROXY, *forwarded) == 403


def test_a_proxy_that_is_not_listed_is_just_a_private_peer(
    make_app: Callable[..., FastAPI],
) -> None:
    """The failure mode the startup docs warn about: without PFA_TRUSTED_PROXIES the proxy's own
    private address is the client, so a public visitor behind it passes the check."""
    app = make_app(policy=ClientPolicy())
    assert _via(app, PROXY, "8.8.8.8") == 200


def test_wildcard_trust_takes_the_rightmost_entry(make_app: Callable[..., FastAPI]) -> None:
    app = make_app(policy=ClientPolicy(), trusted=TrustedProxies.parse("*"))
    assert _via(app, "192.168.1.9", "8.8.8.8, 192.168.1.50") == 200
    assert _via(app, "192.168.1.9", "192.168.1.50, 8.8.8.8") == 403


def test_login_failures_are_counted_per_real_client_behind_a_proxy(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app(policy=ClientPolicy(), trusted=TrustedProxies.parse("172.18.0.0/16"))
    browser = _browser(app, PROXY)

    def attempt(client_ip: str, password: str) -> int:
        return browser.post(
            "/login",
            data={"password": password, "token": TOKEN, "next": "/"},
            headers={"x-forwarded-for": client_ip},
        ).status_code

    for _ in range(5):
        assert attempt("192.168.1.50", "wrong guess") == 401
    assert attempt("192.168.1.50", PASSWORD) == 429  # that client is locked out...
    assert attempt("192.168.1.51", PASSWORD) == 303  # ...but not everyone behind the proxy


def test_forwarded_proto_from_a_trusted_proxy_marks_the_cookie_secure(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app(policy=ClientPolicy(), trusted=TrustedProxies.parse("172.18.0.0/16"))
    browser = _browser(app, PROXY)
    form = {"password": PASSWORD, "token": TOKEN, "next": "/"}
    secure = browser.post(
        "/login",
        data=form,
        headers={"x-forwarded-for": "192.168.1.50", "x-forwarded-proto": "https"},
    )
    assert "secure" in secure.headers["set-cookie"].lower()
    plain = browser.post("/login", data=form, headers={"x-forwarded-for": "192.168.1.50"})
    assert "secure" not in plain.headers["set-cookie"].lower()
    # From an untrusted peer the header is not believed.
    stranger = _browser(app, "192.168.1.9").post(
        "/login", data=form, headers={"x-forwarded-proto": "https"}
    )
    assert "secure" not in stranger.headers["set-cookie"].lower()


# --- escape hatch and unchanged modes --------------------------------------------------------


def test_with_no_policy_the_app_serves_every_address(make_app: Callable[..., FastAPI]) -> None:
    """What PFA_ALLOW_PUBLIC_CLIENTS=1 (policy None) and a plain loopback bind both come to."""
    app = make_app(policy=None)
    assert _browser(app, "8.8.8.8").get("/login").status_code == 200
    assert _browser(app, "testclient").get("/login").status_code == 200


def test_proxy_headers_are_still_applied_without_a_policy(make_app: Callable[..., FastAPI]) -> None:
    app = make_app(policy=None, trusted=TrustedProxies.parse("172.18.0.0/16"))
    assert _via(app, PROXY, "8.8.8.8") == 200  # no policy: nothing to refuse
    assert _via(app, PROXY) == 200  # and a missing header is not an error here


def test_plain_localhost_app_has_no_guard(app_instance: FastAPI) -> None:
    assert not any(m.cls.__name__ == "ClientGuardMiddleware" for m in app_instance.user_middleware)


# --- logging ----------------------------------------------------------------------------------


def test_a_blocked_address_is_logged_once_not_per_request(
    lan_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="personal_finance_analyzer.api.clients")
    browser = _browser(lan_app, "203.0.113.9")
    for _ in range(5):
        assert browser.get("/login").status_code == 403
    lines = [r for r in caplog.records if "203.0.113.9" in r.getMessage()]
    assert len(lines) == 1
    assert "only these client networks are allowed" in lines[0].getMessage()
    assert "192.168.0.0/16" in lines[0].getMessage()


def test_block_log_repeats_after_an_hour_and_caps_the_rate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="personal_finance_analyzer.api.clients")
    now = [0.0]
    log = BlockLog(allowed="private", clock=lambda: now[0], max_per_minute=3)

    def blocked(address: str) -> None:
        log.blocked(ResolvedClient(address, address, False))

    blocked("8.8.8.8")
    blocked("8.8.8.8")
    now[0] += 3601
    blocked("8.8.8.8")  # an hour later: logged again
    assert len(caplog.records) == 2
    caplog.clear()

    now[0] += 3601
    for i in range(10):
        blocked(f"203.0.113.{i}")  # a scan: only three lines get through
    assert len(caplog.records) == 3
    caplog.clear()
    now[0] += 61
    blocked("198.51.100.1")
    assert len(caplog.records) == 1
    assert "7 more refused" in caplog.records[0].getMessage()


def test_block_log_names_a_proxy_with_no_forwarded_header(
    proxied_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="personal_finance_analyzer.api.clients")
    assert _via(proxied_app, PROXY) == 403
    assert any(
        PROXY in r.getMessage() and "X-Forwarded-For" in r.getMessage() for r in caplog.records
    )


def test_log_never_echoes_header_text(
    proxied_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="personal_finance_analyzer.api.clients")
    assert _via(proxied_app, PROXY, "evil;INJECTED") == 403
    assert "INJECTED" not in caplog.text


def test_hint_when_an_untrusted_private_peer_forwards_headers(
    make_app: Callable[..., FastAPI], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="personal_finance_analyzer.api.clients")
    app = make_app(policy=ClientPolicy())
    for _ in range(3):
        _via(app, PROXY, "8.8.8.8")
    hints = [r for r in caplog.records if "PFA_TRUSTED_PROXIES" in r.getMessage()]
    assert len(hints) == 1
    assert PROXY in hints[0].getMessage()


# --- Docker gateway detection ---------------------------------------------------------------

ROUTE_TABLE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
)


def test_default_gateway_is_read_from_the_route_table() -> None:
    assert default_gateway(ROUTE_TABLE) == "172.18.0.1"


@pytest.mark.parametrize(
    "table",
    [
        "",
        "Iface\tDestination\tGateway\tFlags\n",
        # a default route without the gateway flag (a point-to-point link) has no gateway
        "Iface\tDestination\tGateway\tFlags\neth0\t00000000\t0100A8C0\t0001\n",
        "Iface\tDestination\tGateway\tFlags\neth0\t00000000\t00000000\t0003\n",
        "Iface\tDestination\tGateway\tFlags\neth0\t00000000\tzzzz\t0003\n",
        "garbage",
    ],
)
def test_default_gateway_is_none_when_there_is_none(table: str) -> None:
    assert default_gateway(table) is None


def test_default_gateway_reads_proc_when_not_given_a_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    route = tmp_path / "route"
    route.write_text(ROUTE_TABLE, encoding="ascii")
    monkeypatch.setattr("personal_finance_analyzer.api.clients.ROUTE_TABLE", route)
    assert default_gateway() == "172.18.0.1"
    monkeypatch.setattr("personal_finance_analyzer.api.clients.ROUTE_TABLE", tmp_path / "missing")
    assert default_gateway() is None


def _guarded(gateway: str | None) -> FastAPI:
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def ok(_request: object) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = FastAPI(routes=[Route("/", ok)])
    app.add_middleware(
        ClientGuardMiddleware,
        policy=ClientPolicy(),
        block_log=BlockLog(allowed="private"),
        gateway=gateway,
    )
    return app


def test_a_request_from_the_container_gateway_logs_a_one_time_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="personal_finance_analyzer.api.clients")
    app = _guarded("172.18.0.1")
    for _ in range(3):
        assert _browser(app, "172.18.0.1").get("/").status_code == 200
    lines = [r.getMessage() for r in caplog.records if "default gateway" in r.getMessage()]
    assert len(lines) == 1
    assert "172.18.0.1" in lines[0]
    assert "0.0.0.0:8756:8756" in lines[0]


def test_other_peers_do_not_trigger_the_gateway_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="personal_finance_analyzer.api.clients")
    app = _guarded("172.18.0.1")
    assert _browser(app, "192.168.1.50").get("/").status_code == 200
    assert _browser(app, "172.18.0.7").get("/").status_code == 200
    assert not [r for r in caplog.records if "default gateway" in r.getMessage()]
    # and without a known gateway there is nothing to compare with
    quiet = _guarded(None)
    assert _browser(quiet, "172.18.0.1").get("/").status_code == 200
    assert not [r for r in caplog.records if "default gateway" in r.getMessage()]


def test_the_app_passes_the_detected_gateway_to_the_guard(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="personal_finance_analyzer.api.clients")
    monkeypatch.setattr("personal_finance_analyzer.api.app.default_gateway", lambda: "172.18.0.1")
    app = make_app(policy=ClientPolicy())
    assert _browser(app, "172.18.0.1").get("/login").status_code == 200
    assert any("default gateway" in r.getMessage() for r in caplog.records)
