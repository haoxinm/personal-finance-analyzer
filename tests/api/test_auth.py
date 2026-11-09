"""The single-password login that guards a server listening beyond loopback (`api/auth.py`), the
widened Host allowlist, the health probe, and the guarantee that the plain localhost app is
unchanged."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from personal_finance_analyzer.api.app import create_app
from personal_finance_analyzer.api.auth import (
    SESSION_COOKIE,
    AuthConfig,
    AuthConfigError,
    SessionStore,
    hash_password,
    load_password_hash,
    parse_password_hash,
    safe_next,
    verify_password,
)
from personal_finance_analyzer.api.security import HostPolicy
from personal_finance_analyzer.config import Settings

PASSWORD = "correct horse battery"
TOKEN = "test-token"
HOST = "nas.local:8756"
INDEX = "<html><head><title>x</title></head><body>app</body></html>"

# One real scrypt hash shared by every test (each hash costs ~100 ms).
PASSWORD_HASH = hash_password(PASSWORD)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def web_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "web"
    directory.mkdir()
    (directory / "index.html").write_text(INDEX, encoding="utf-8")
    return directory


@pytest.fixture
def auth_app(engine: Engine, session: Session, web_dir: Path, clock: FakeClock) -> FastAPI:
    del session  # taxonomy synced
    return create_app(
        engine=engine,
        settings=Settings(),
        token=TOKEN,
        web_dir=web_dir,
        auth=AuthConfig(password_hash=PASSWORD_HASH, clock=clock),
        allowed_hosts=("nas.local",),
    )


@pytest.fixture
def anon(auth_app: FastAPI) -> Iterator[TestClient]:
    """A browser that has not signed in, talking to the NAS by name."""
    with TestClient(auth_app, base_url="http://nas.local:8756", follow_redirects=False) as c:
        yield c


def sign_in(client: TestClient, password: str = PASSWORD, next_path: str = "/") -> object:
    return client.post("/login", data={"password": password, "token": TOKEN, "next": next_path})


@pytest.fixture
def signed_in(anon: TestClient) -> TestClient:
    response = sign_in(anon)
    assert response.status_code == 303  # type: ignore[attr-defined]
    return anon


# --- password hashing ---------------------------------------------------------------------


def test_hash_round_trip_and_salting() -> None:
    first, second = hash_password("a long password"), hash_password("a long password")
    assert first != second  # random salt
    assert verify_password("a long password", first)
    assert not verify_password("a long passwore", first)
    assert not verify_password("", first)


def test_hash_does_not_contain_the_password() -> None:
    assert "horse" not in PASSWORD_HASH


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "plaintext",
        "scrypt$15$8$1$only-four",
        "scrypt$x$8$1$AAAA$AAAA",
        "scrypt$15$8$1$!!!$AAAA",
        "scrypt$40$8$1$AAAA$AAAA",  # absurd cost: refused, not computed
        "bcrypt$15$8$1$AAAA$AAAA",
    ],
)
def test_malformed_hash_fails_closed(bad: str) -> None:
    assert not verify_password("anything", bad)
    with pytest.raises(ValueError):
        parse_password_hash(bad)


def test_unicode_password_round_trip() -> None:
    encoded = hash_password("pässwörd-密码-ok")
    assert verify_password("pässwörd-密码-ok", encoded)


# --- reading the password from the environment --------------------------------------------


def test_no_password_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_password_hash() is None


def test_plain_password_is_hashed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    encoded = load_password_hash()
    assert encoded is not None
    assert encoded != PASSWORD
    assert verify_password(PASSWORD, encoded)


def test_password_from_a_docker_secret_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = tmp_path / "pfa_password"
    secret.write_text(PASSWORD + "\n", encoding="utf-8")
    monkeypatch.setenv("PFA_PASSWORD_FILE", str(secret))
    encoded = load_password_hash()
    assert encoded is not None
    assert verify_password(PASSWORD, encoded)


def test_prehashed_password_is_used_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_PASSWORD_HASH", PASSWORD_HASH)
    assert load_password_hash() == PASSWORD_HASH


def test_short_password_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_PASSWORD", "short")
    with pytest.raises(AuthConfigError, match="at least 8"):
        load_password_hash()


def test_password_and_hash_together_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_PASSWORD", PASSWORD)
    monkeypatch.setenv("PFA_PASSWORD_HASH", PASSWORD_HASH)
    with pytest.raises(AuthConfigError, match="not both"):
        load_password_hash()


def test_invalid_hash_and_unreadable_file_are_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PFA_PASSWORD_HASH", "nope")
    with pytest.raises(AuthConfigError, match="hash-password"):
        load_password_hash()
    monkeypatch.delenv("PFA_PASSWORD_HASH")
    monkeypatch.setenv("PFA_PASSWORD_FILE", str(tmp_path / "missing"))
    with pytest.raises(AuthConfigError, match="PFA_PASSWORD_FILE"):
        load_password_hash()


# --- host allowlist -----------------------------------------------------------------------


def test_host_policy() -> None:
    policy = HostPolicy(8756, ["NAS.local", "192.168.1.10:9000", "[fd00::1]"])
    assert policy.allows("127.0.0.1")
    assert policy.allows("localhost:8756")
    assert not policy.allows("localhost:9999")  # a loopback name only on the bound port
    assert policy.allows("nas.local")
    assert policy.allows("nas.local:1234")  # a published port can differ from the bound one
    assert policy.allows("192.168.1.10:9000")
    assert not policy.allows("192.168.1.10:8756")
    assert not policy.allows("192.168.1.10")
    assert policy.allows("[fd00::1]:8756")
    assert not policy.allows("evil.example")
    assert not policy.allows("")
    assert HostPolicy(8756, ["*"]).allows("anything.example")


def test_listed_host_is_accepted_without_a_login(
    engine: Engine, session: Session, tmp_path: Path
) -> None:
    del session
    app = create_app(
        engine=engine,
        settings=Settings(),
        token=TOKEN,
        web_dir=tmp_path / "none",
        allowed_hosts=("nas.local",),
    )
    with TestClient(app, base_url="http://nas.local:8756") as client:
        assert client.get("/api/users", headers={"X-PFA-Token": TOKEN}).status_code == 200
        assert (
            client.get(
                "/api/users", headers={"X-PFA-Token": TOKEN, "Host": "evil.example"}
            ).status_code
            == 403
        )


# --- the plain localhost app is unchanged -------------------------------------------------


def test_default_app_has_no_login(engine: Engine, session: Session, web_dir: Path) -> None:
    del session
    app = create_app(engine=engine, settings=Settings(), token=TOKEN, web_dir=web_dir)
    assert not hasattr(app.state, "auth")
    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as client:
        page = client.get("/", headers={"Host": "127.0.0.1"})
        assert page.status_code == 200
        assert "pfa-auth" not in page.text
        # /login is not a route here: it falls through to the SPA like any client-side path.
        assert 'name="password"' not in client.get("/login", headers={"Host": "127.0.0.1"}).text
        assert client.get("/api/users", headers={"Host": "127.0.0.1"}).status_code == 403
        ok = client.get("/api/users", headers={"Host": "127.0.0.1", "X-PFA-Token": TOKEN})
        assert ok.status_code == 200
        assert client.get("/api/users", headers={"Host": "nas.local"}).status_code == 403


def test_health_is_open_and_reveals_nothing(client: TestClient) -> None:
    # No token, and a Host that the allowlist would refuse anywhere else.
    response = client.get("/api/health", headers={"X-PFA-Token": "", "Host": "anything.example"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- the login ----------------------------------------------------------------------------


def test_anonymous_page_load_redirects_to_login(anon: TestClient) -> None:
    response = anon.get("/tax/documents?year=2025")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Ftax%2Fdocuments%3Fyear%3D2025"
    assert response.headers["cache-control"] == "no-store"


def test_anonymous_api_is_401_even_with_a_valid_token(anon: TestClient) -> None:
    assert anon.get("/api/users", headers={"X-PFA-Token": TOKEN}).status_code == 401
    assert anon.get("/api/users").status_code == 401
    posted = anon.post("/api/users", json={"name": "x"}, headers={"X-PFA-Token": TOKEN})
    assert posted.status_code == 401
    assert anon.get("/assets/app.js").status_code == 303  # even static files wait for the login


def test_health_needs_no_login(anon: TestClient) -> None:
    response = anon.get("/api/health", headers={"Host": "127.0.0.1:8756"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert anon.get("/api/health").status_code == 200


def test_login_page(anon: TestClient) -> None:
    response = anon.get("/login?next=/tax")
    assert response.status_code == 200
    assert 'type="password"' in response.text
    assert f'name="token" value="{TOKEN}"' in response.text
    assert 'name="next" value="/tax"' in response.text
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_wrong_host_is_refused_everywhere_but_health(anon: TestClient) -> None:
    assert anon.get("/login", headers={"Host": "evil.example"}).status_code == 403
    assert anon.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert anon.get("/api/health", headers={"Host": "evil.example"}).status_code == 200


def test_wrong_password(anon: TestClient) -> None:
    response = sign_in(anon, "not the password")
    assert response.status_code == 401  # type: ignore[attr-defined]
    assert "Incorrect password" in response.text  # type: ignore[attr-defined]
    assert SESSION_COOKIE not in anon.cookies
    assert anon.get("/api/users", headers={"X-PFA-Token": TOKEN}).status_code == 401


def test_empty_password(anon: TestClient) -> None:
    assert sign_in(anon, "").status_code == 401  # type: ignore[attr-defined]


def test_successful_login_sets_a_hardened_cookie(anon: TestClient) -> None:
    response = sign_in(anon, next_path="/tax")
    assert response.status_code == 303  # type: ignore[attr-defined]
    assert response.headers["location"] == "/tax"  # type: ignore[attr-defined]
    cookie = response.headers["set-cookie"]  # type: ignore[attr-defined]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "path=/" in lowered
    assert "secure" not in lowered  # plain HTTP on the LAN: a Secure cookie would never come back
    assert "max-age" not in lowered  # a browser-session cookie
    assert PASSWORD not in cookie


def test_cookie_is_secure_when_the_request_arrived_over_https(auth_app: FastAPI) -> None:
    with TestClient(auth_app, base_url="https://nas.local", follow_redirects=False) as client:
        response = sign_in(client)
        assert "secure" in response.headers["set-cookie"].lower()  # type: ignore[attr-defined]


def test_cookie_can_be_forced_secure(engine: Engine, session: Session, web_dir: Path) -> None:
    del session
    app = create_app(
        engine=engine,
        settings=Settings(),
        token=TOKEN,
        web_dir=web_dir,
        auth=AuthConfig(password_hash=PASSWORD_HASH, force_secure_cookie=True),
        allowed_hosts=("nas.local",),
    )
    with TestClient(app, base_url="http://nas.local:8756", follow_redirects=False) as client:
        assert "secure" in sign_in(client).headers["set-cookie"].lower()  # type: ignore[attr-defined]


def test_signed_in_user_gets_the_spa_and_the_api(signed_in: TestClient) -> None:
    page = signed_in.get("/")
    assert page.status_code == 200
    assert f'name="pfa-token" content="{TOKEN}"' in page.text
    assert 'name="pfa-auth" content="1"' in page.text  # tells the UI to show Sign out
    assert signed_in.get("/some/client/route").status_code == 200
    assert signed_in.get("/api/users", headers={"X-PFA-Token": TOKEN}).status_code == 200


def test_the_per_launch_token_still_applies_behind_the_login(signed_in: TestClient) -> None:
    assert signed_in.get("/api/users").status_code == 403
    assert signed_in.get("/api/users", headers={"X-PFA-Token": "wrong"}).status_code == 403
    non_ascii = {"X-PFA-Token": "töken".encode()}  # must be a refusal, not a 500
    assert signed_in.get("/api/users", headers=non_ascii).status_code == 403
    assert (
        signed_in.get("/api/users", headers={"X-PFA-Token": TOKEN, "Host": "evil.example"})
    ).status_code == 403
    upload = signed_in.post(
        "/api/statements",
        data={"user_id": "1"},
        files={"file": ("s.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert upload.status_code == 403


def test_login_page_visit_when_already_signed_in_goes_to_the_app(signed_in: TestClient) -> None:
    response = signed_in.get("/login?next=/tax")
    assert response.status_code == 303
    assert response.headers["location"] == "/tax"


def test_login_form_needs_the_current_token(anon: TestClient) -> None:
    response = anon.post("/login", data={"password": PASSWORD, "token": "stale", "next": "/"})
    assert response.status_code == 403
    assert "expired" in response.text
    assert SESSION_COOKIE not in anon.cookies
    # no token at all
    assert anon.post("/login", data={"password": PASSWORD}).status_code == 403


def test_cross_origin_login_post_is_refused(anon: TestClient) -> None:
    response = anon.post(
        "/login",
        data={"password": PASSWORD, "token": TOKEN},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert SESSION_COOKIE not in anon.cookies
    same_origin = anon.post(
        "/login",
        data={"password": PASSWORD, "token": TOKEN},
        headers={"Origin": "http://nas.local:8756"},
    )
    assert same_origin.status_code == 303


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/",
        "//evil.example/",
        "/\\evil.example",
        "javascript:alert(1)",
        "/login",
    ],
)
def test_next_cannot_leave_the_site(anon: TestClient, target: str) -> None:
    response = sign_in(anon, next_path=target)
    assert response.status_code == 303  # type: ignore[attr-defined]
    assert response.headers["location"] == "/"  # type: ignore[attr-defined]


def test_safe_next_keeps_ordinary_paths() -> None:
    assert safe_next("/tax/documents?year=2025") == "/tax/documents?year=2025"
    assert safe_next(None) == "/"
    assert safe_next("") == "/"
    assert safe_next("/a\nb") == "/"


def test_next_is_html_escaped_in_the_login_page(anon: TestClient) -> None:
    response = anon.get('/login?next=/"><script>alert(1)</script>')
    assert "<script>alert(1)</script>" not in response.text


# --- rate limiting ------------------------------------------------------------------------


def test_login_is_rate_limited_per_client(anon: TestClient, clock: FakeClock) -> None:
    for _ in range(5):
        assert sign_in(anon, "wrong guess").status_code == 401  # type: ignore[attr-defined]
    locked = sign_in(anon, "wrong guess")
    assert locked.status_code == 429  # type: ignore[attr-defined]
    assert int(locked.headers["retry-after"]) > 0  # type: ignore[attr-defined]
    # The right password does not get in while locked out.
    assert sign_in(anon).status_code == 429  # type: ignore[attr-defined]
    assert SESSION_COOKIE not in anon.cookies

    clock.advance(299)
    assert sign_in(anon).status_code == 429  # type: ignore[attr-defined]
    clock.advance(5)
    assert sign_in(anon).status_code == 303  # type: ignore[attr-defined]


def test_a_successful_login_clears_that_clients_failures(anon: TestClient) -> None:
    for _ in range(4):
        sign_in(anon, "wrong guess")
    assert sign_in(anon).status_code == 303  # type: ignore[attr-defined]
    anon.cookies.clear()
    for _ in range(4):
        assert sign_in(anon, "wrong guess").status_code == 401  # type: ignore[attr-defined]


def test_global_limit_stops_a_distributed_guesser(
    engine: Engine, session: Session, web_dir: Path, clock: FakeClock
) -> None:
    del session
    app = create_app(
        engine=engine,
        settings=Settings(),
        token=TOKEN,
        web_dir=web_dir,
        auth=AuthConfig(
            password_hash=PASSWORD_HASH,
            clock=clock,
            max_failures_per_client=100,
            max_failures_global=3,
        ),
        allowed_hosts=("nas.local",),
    )
    with TestClient(app, base_url="http://nas.local:8756", follow_redirects=False) as client:
        for _ in range(3):
            assert sign_in(client, "wrong guess").status_code == 401  # type: ignore[attr-defined]
        assert sign_in(client).status_code == 429  # type: ignore[attr-defined]


# --- session lifetime ---------------------------------------------------------------------


def test_session_expires_when_idle(signed_in: TestClient, clock: FakeClock) -> None:
    headers = {"X-PFA-Token": TOKEN}
    assert signed_in.get("/api/users", headers=headers).status_code == 200
    clock.advance(8 * 3600 + 1)
    assert signed_in.get("/api/users", headers=headers).status_code == 401
    assert signed_in.get("/").status_code == 303
    # The dead session stays dead even if the clock is wound back.
    clock.now -= 8 * 3600
    assert signed_in.get("/api/users", headers=headers).status_code == 401


def test_activity_keeps_a_session_alive_until_its_maximum_age(
    signed_in: TestClient, clock: FakeClock
) -> None:
    headers = {"X-PFA-Token": TOKEN}
    for _ in range(30):  # a request every 7 h, never idle for 8 h
        clock.advance(7 * 3600)
        status = signed_in.get("/api/users", headers=headers).status_code
        if status == 401:
            break
    else:
        pytest.fail("the absolute maximum age never ended the session")
    assert clock.now - 1000.0 > 7 * 24 * 3600  # not before the 7-day maximum


def test_logout_ends_the_session_and_needs_the_token(signed_in: TestClient) -> None:
    assert signed_in.post("/logout").status_code == 403  # a forged cross-site POST has no token
    assert signed_in.get("/api/users", headers={"X-PFA-Token": TOKEN}).status_code == 200

    response = signed_in.post("/logout", headers={"X-PFA-Token": TOKEN})
    assert response.status_code == 200
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert signed_in.get("/api/users", headers={"X-PFA-Token": TOKEN}).status_code == 401


def test_logout_revokes_the_server_side_session(signed_in: TestClient) -> None:
    stolen = signed_in.cookies[SESSION_COOKIE]
    signed_in.post("/logout", headers={"X-PFA-Token": TOKEN})
    signed_in.cookies.set(SESSION_COOKIE, stolen)  # replaying the old cookie must not work
    assert signed_in.get("/api/users", headers={"X-PFA-Token": TOKEN}).status_code == 401


def test_forged_and_garbage_cookies_are_rejected(anon: TestClient) -> None:
    anon.cookies.set(SESSION_COOKIE, "guess")
    assert anon.get("/api/users", headers={"X-PFA-Token": TOKEN}).status_code == 401
    anon.cookies.set(SESSION_COOKIE, "")
    assert anon.get("/").status_code == 303


def test_logout_without_a_session_is_401(anon: TestClient) -> None:
    assert anon.post("/logout", headers={"X-PFA-Token": TOKEN}).status_code == 401


def test_session_store_is_bounded_and_independent() -> None:
    store = SessionStore(idle_seconds=60, max_seconds=600)
    ids = [store.create() for _ in range(SessionStore.MAX_SESSIONS + 5)]
    assert len(set(ids)) == len(ids)
    assert not store.valid(ids[0])  # the oldest was dropped
    assert store.valid(ids[-1])
    store.clear()
    assert not store.valid(ids[-1])
    assert not store.valid(None)
