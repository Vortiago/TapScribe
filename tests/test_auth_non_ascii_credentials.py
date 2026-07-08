"""RED contract for issue #194 — `hmac.compare_digest` raises `TypeError`
for `str` arguments containing non-ASCII characters, and none of
`tapscribe/auth.py`'s three credential-comparison call sites
(`pick_tap_subprotocol`, `check_tap_bearer`, and the Basic branch of
`basic_auth_middleware`) catches it. A Basic password may legitimately
contain non-ASCII (decoded as UTF-8); a bearer/subprotocol token can end up
non-ASCII too, since ASGI decodes headers as latin-1 and bytes >0x7F yield a
non-ASCII `str`. Any request carrying such credentials crashes the
comparison with an unhandled 500 instead of a clean 401 — on every request
path, unauthenticated (see `test_auth.py` for the mini-app + scheme
dispatch this reuses).

The fix (per the issue): encode both sides to bytes before comparing. Each
call site gets two tests here — a WRONG non-ASCII credential must still be
cleanly rejected, and a legitimately-configured non-ASCII credential that
actually MATCHES must be accepted — neither may raise.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_auth import BASIC_PASS, TAP_TOKEN, _mini_app  # type: ignore[import-not-found]

from tapscribe import auth
from tapscribe import config as _config

NON_ASCII_PASSWORD = "pässwörd-üñíçødé"
NON_ASCII_TOKEN = "tökén-sécret-üñíçødé"


def _basic_header(user: str, password: str) -> dict[str, str]:
    creds = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {creds}"}


# --- pick_tap_subprotocol (auth.py:48) --------------------------------------


def test_pick_tap_subprotocol_rejects_wrong_non_ascii_token_without_crashing() -> None:
    offered = [auth.TAP_SUBPROTOCOL_PREFIX + "wrong-" + NON_ASCII_TOKEN]
    assert auth.pick_tap_subprotocol(offered, NON_ASCII_TOKEN) is None


def test_pick_tap_subprotocol_accepts_matching_non_ascii_token() -> None:
    proto = auth.TAP_SUBPROTOCOL_PREFIX + NON_ASCII_TOKEN
    assert auth.pick_tap_subprotocol([proto], NON_ASCII_TOKEN) == proto


# --- check_tap_bearer (auth.py:70) ------------------------------------------


def test_check_tap_bearer_rejects_wrong_non_ascii_token_without_crashing() -> None:
    assert auth.check_tap_bearer(f"Bearer wrong-{NON_ASCII_TOKEN}", NON_ASCII_TOKEN) is False


def test_check_tap_bearer_accepts_matching_non_ascii_token() -> None:
    assert auth.check_tap_bearer(f"Bearer {NON_ASCII_TOKEN}", NON_ASCII_TOKEN) is True


# --- basic_auth_middleware's Basic branch (auth.py:135-136) -----------------


@pytest.fixture
def client_with_non_ascii_password(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(_config, "AUTH_ENABLED", True)
    app = _mini_app()
    app.state.recorder = SimpleNamespace(
        tap=SimpleNamespace(value=TAP_TOKEN),
        auth=SimpleNamespace(value=NON_ASCII_PASSWORD),
    )
    # raise_server_exceptions=False: the bug under test crashes the
    # middleware with an unhandled TypeError, which TestClient would
    # otherwise re-raise into the test instead of surfacing the 500 the
    # issue actually reports reaching the client.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_basic_auth_rejects_wrong_non_ascii_password_without_crashing(
    client_with_non_ascii_password: TestClient,
) -> None:
    r = client_with_non_ascii_password.get(
        "/api/whatever", headers=_basic_header(_config.AUTH_USER, "wrong-" + NON_ASCII_PASSWORD)
    )
    assert r.status_code == 401


def test_basic_auth_accepts_matching_non_ascii_password(
    client_with_non_ascii_password: TestClient,
) -> None:
    r = client_with_non_ascii_password.get(
        "/api/whatever", headers=_basic_header(_config.AUTH_USER, NON_ASCII_PASSWORD)
    )
    assert r.status_code == 200


def test_basic_auth_still_works_for_the_pre_existing_ascii_only_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-regression: the plain-ASCII path `test_auth.py` already pins
    must keep working once both sides are bytes-encoded before comparing."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", True)
    with TestClient(_mini_app()) as c:
        ok = c.get("/api/whatever", auth=(_config.AUTH_USER, BASIC_PASS))
        assert ok.status_code == 200
