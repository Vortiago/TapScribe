"""The two login-link routes, behind the real auth middleware.

`test_login_links.py` covers the store's state machine; this covers what the HTTP
surface does with it — the exempt route, the cookie's attributes, and the one
rule that matters most: a spent or expired link answers with a PAGE and never
with a bare 401, because a 401 on a navigation is what pops the browser's native
Basic dialog (ADR-0023).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from test_auth import BASIC_PASS as PASSWORD
from test_auth import _mini_app

from tapscribe import config as _config
from tapscribe.config import session_cookie_name as cookie_name
from tapscribe.login_links import LoginLinks
from tapscribe.routes.login import router as login_router


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The two routes behind the real auth middleware, and nothing else.

    `test_auth`'s mini app IS that scaffold — middleware, a fake recorder, a
    store — so the login router is added to it rather than a second copy being
    built here. A copy had already drifted: it named the tap token differently
    from the middleware's own tests. The global app would drag in the lifespan
    and a live Recorder, neither of which these routes touch.
    """
    monkeypatch.setattr(_config, "AUTH_ENABLED", True)
    app = _mini_app()
    app.include_router(login_router)
    with TestClient(app) as c:
        yield c


def _mint(client: TestClient) -> str:
    r = client.post("/api/login-link", auth=(_config.AUTH_USER, PASSWORD))
    assert r.status_code == 200, r.text
    return r.json()["path"]


def test_minting_a_link_needs_the_password(client: TestClient) -> None:
    """The whole access control: only a caller that could already reach the
    dashboard can make a way in. The tray is that caller — it reads
    `.auth-password` off disk for Copy password already."""
    assert client.post("/api/login-link").status_code == 401


def test_a_minted_link_is_a_path_not_a_url(client: TestClient) -> None:
    """The tray knows the host and port it supervises; a Recorder behind a proxy
    would guess wrong."""
    path = _mint(client)
    assert path.startswith("/login?k=")


def test_spending_a_link_sets_the_session_cookie_and_lands_on_the_dashboard(
    client: TestClient,
) -> None:
    r = client.get(_mint(client), follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookie = r.cookies.get(cookie_name())
    assert cookie
    assert client.app.state.login_links.validate(cookie)


def test_the_cookie_is_httponly_and_samesite_strict(client: TestClient) -> None:
    """HttpOnly keeps a script that lands on the page from reading the session;
    SameSite=Strict is what stops the browser ATTACHING it to a cross-site
    request at all, navigation and fetch alike — server CORS config never
    governs sending."""
    r = client.get(_mint(client), follow_redirects=False)

    header = r.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=strict" in header
    # No Max-Age/Expires: a session cookie, because the store is in memory and
    # a browser holding it longer than the process did would just mean a 401.
    assert "max-age" not in header and "expires" not in header


def test_secure_follows_tls(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting it unconditionally would make the cookie unusable over plain
    http, which is how a Bundle serves by default."""
    plain = client.get(_mint(client), follow_redirects=False)
    assert "secure" not in plain.headers["set-cookie"].lower()

    monkeypatch.setattr(_config, "TLS_ENABLED", True)
    secured = client.get(_mint(client), follow_redirects=False)
    assert "secure" in secured.headers["set-cookie"].lower()


def test_the_cookie_name_carries_the_port(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cookies are scoped by host but NOT by port, so a checkout on 8001 and a
    second Recorder on 8002 would otherwise clobber each other's session."""
    monkeypatch.setattr(_config, "PORT", 8002)
    r = client.get(_mint(client), follow_redirects=False)

    assert "tapscribe_session_8002=" in r.headers["set-cookie"]


def test_a_spent_link_renders_a_page_and_never_a_bare_401(client: TestClient) -> None:
    """THE rule. A 401 from a navigation pops the browser's native Basic dialog,
    which is the prompt this whole feature exists to remove — so the dead-link
    answer is a 200 the operator can read."""
    path = _mint(client)
    client.get(path, follow_redirects=False)

    # Past the grace window, by emptying the store rather than by waiting: the
    # store's own clock handling is unit-tested, and this is about the ROUTE.
    client.app.state.login_links = LoginLinks()

    spent = client.get(path, follow_redirects=False)
    assert spent.status_code == 200
    assert "text/html" in spent.headers["content-type"]
    assert "used up" in spent.text.lower()
    assert "WWW-Authenticate" not in spent.headers


def test_the_login_route_needs_no_credential_of_its_own(client: TestClient) -> None:
    """It is in `AUTH_EXEMPT_ROUTES` because it AUTHENTICATES BY SPENDING its
    token: demanding a second credential first would defeat the link."""
    r = client.get("/login?k=nope", follow_redirects=False)
    assert r.status_code == 200

    # ...and being exempt buys nothing else: no cookie is set for a bad token.
    assert cookie_name() not in r.cookies
