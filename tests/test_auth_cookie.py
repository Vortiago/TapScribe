"""The BASIC scheme's second credential form: the dashboard session cookie.

ADR-0023 amends ADR-0008 by adding a FORM, not a scheme — the middleware accepts
a session cookie exactly where it accepts an `Authorization: Basic` header, and
no route is gated differently by which of the two a caller used. Its own module
beside `test_auth.py` (like `test_auth_non_ascii_credentials.py`) because it also
covers the two rules that ride along with the cookie: the omitted
`WWW-Authenticate`, and the Origin check on state-changing requests.

Driven through `test_auth`'s mini app, so these pin the same middleware seam the
three-scheme tests do, with no real route in the way.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from test_auth import BASIC_PASS, TAP_TOKEN, _mini_app

from tapscribe import config as _config
from tapscribe.routes.login import cookie_name


@pytest.fixture
def auth_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(_config, "AUTH_ENABLED", True)
    with TestClient(_mini_app()) as c:
        yield c


def _sign_in(client: TestClient) -> str:
    """Mint and spend a link, and put the session on the CLIENT — httpx deprecates
    per-request cookies, and a cookie jar is what a browser has anyway."""
    links = client.app.state.login_links
    cookie = links.spend(links.mint())
    assert cookie
    client.cookies.set(cookie_name(), cookie)
    return cookie


def test_a_session_cookie_is_accepted_where_basic_is(auth_on: TestClient) -> None:
    _sign_in(auth_on)

    ok = auth_on.get("/api/whatever")

    assert ok.status_code == 200
    assert ok.json()["scheme"] == "basic"


def test_a_cookie_this_recorder_never_issued_is_refused(auth_on: TestClient) -> None:
    # The restart case: the store is in memory, so yesterday's session is not a
    # credential today.
    auth_on.cookies.set(cookie_name(), "not-a-session")
    assert auth_on.get("/api/whatever").status_code == 401


def test_a_cookie_does_not_open_the_tap_bearer_scheme(auth_on: TestClient) -> None:
    """ADR-0008's three schemes are untouched: a dashboard session is not a tap
    token, any more than a Basic header is."""
    _sign_in(auth_on)

    assert auth_on.get("/api/tap/probe").status_code == 401


def test_a_401_omits_www_authenticate_when_a_cookie_was_presented(auth_on: TestClient) -> None:
    """Otherwise a Recorder restart turns the dashboard's 500 ms poll into the
    browser's native Basic dialog — the one prompt the login link removes."""
    auth_on.cookies.set(cookie_name(), "stale")
    stale = auth_on.get("/api/whatever")
    assert stale.status_code == 401
    assert "WWW-Authenticate" not in stale.headers

    # ...and a caller that presented NO cookie still gets the challenge, so a
    # curl user is not left guessing.
    auth_on.cookies.clear()
    bare = auth_on.get("/api/whatever")
    assert bare.status_code == 401
    assert "WWW-Authenticate" in bare.headers


def test_a_cross_origin_write_is_refused_on_the_basic_scheme(auth_on: TestClient) -> None:
    """SameSite=Strict stops a cross-SITE page attaching the cookie, but site
    scoping ignores PORTS: a hostile page on another localhost port is same-site
    to this one. The Origin check is what closes that."""
    r = auth_on.post(
        "/api/whatever",
        auth=(_config.AUTH_USER, BASIC_PASS),
        headers={"Origin": "http://localhost:9999"},
    )
    assert r.status_code == 403


def test_a_same_origin_write_and_an_origin_less_one_both_pass(auth_on: TestClient) -> None:
    same = auth_on.post(
        "/api/whatever",
        auth=(_config.AUTH_USER, BASIC_PASS),
        headers={"Origin": "http://testserver"},
    )
    assert same.status_code == 200

    # No Origin at all: curl, the tray, every other non-browser caller.
    assert auth_on.post("/api/whatever", auth=(_config.AUTH_USER, BASIC_PASS)).status_code == 200


def test_a_cross_origin_read_still_passes(auth_on: TestClient) -> None:
    """Reads are protected by not being READABLE cross-origin — `allow_credentials`
    is false — where a write executes whether or not the attacker sees the answer.
    Refusing reads too would buy nothing and break the extension's /health probe
    shape for no reason."""
    r = auth_on.get(
        "/api/whatever",
        auth=(_config.AUTH_USER, BASIC_PASS),
        headers={"Origin": "http://localhost:9999"},
    )
    assert r.status_code == 200


def test_the_origin_check_does_not_reach_the_tap_bearer_plane(auth_on: TestClient) -> None:
    """The SpatialChat bridge POSTs to /api/tap/* from spatial.chat, and
    `allow_origins=["*"]` is load-bearing for it. Widening the check to that
    branch would break the one bridge that needs it."""
    r = auth_on.post(
        "/api/tap/write",
        headers={"Authorization": "Bearer " + TAP_TOKEN, "Origin": "https://spatial.chat"},
    )
    assert r.status_code == 200
    assert r.json()["scheme"] == "tap"
