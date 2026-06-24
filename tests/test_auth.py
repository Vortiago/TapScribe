"""The HTTP auth gate: one middleware dispatching every request to exactly
one of three schemes — public / tap-bearer / Basic (see CONTEXT.md "HTTP
auth gate · auth schemes" and ADR-0008).

These tests drive the middleware seam directly with a minimal app and a
fake recorder (the middleware only reads ``recorder.auth.value`` /
``recorder.tap.value``), so they pin the dispatch independent of any real
route. The structural guarantee under test: a route under ``TAP_PREFIX``
with NO handler-level gate is STILL rejected without a valid tap bearer —
the gate lives in the middleware, so a future tap route is gated by
construction.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tapscribe import auth
from tapscribe import config as _config

TAP_TOKEN = "tap-secret-token"
BASIC_PASS = "basic-secret-pass"


def _mini_app() -> FastAPI:
    """A throwaway app wired with ONLY the auth middleware + one route per
    scheme, none of which carry their own auth gate."""
    app = FastAPI()
    app.middleware("http")(auth.basic_auth_middleware)

    @app.get("/api/tap/probe")  # tap-bearer scheme — deliberately gate-less
    async def _tap_probe() -> dict:
        return {"scheme": "tap"}

    @app.get("/health")  # public scheme (exact-exempt)
    async def _health() -> dict:
        return {"scheme": "public"}

    @app.get("/api/whatever")  # Basic scheme (default)
    async def _whatever() -> dict:
        return {"scheme": "basic"}

    app.state.recorder = SimpleNamespace(
        tap=SimpleNamespace(value=TAP_TOKEN),
        auth=SimpleNamespace(value=BASIC_PASS),
    )
    return app


@pytest.fixture
def auth_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(_config, "AUTH_ENABLED", True)
    with TestClient(_mini_app()) as c:
        yield c


def test_gateless_tap_route_is_rejected_by_the_middleware(auth_on: TestClient) -> None:
    """The deepening's core guarantee: a route under TAP_PREFIX that carries
    NO handler gate is still 401'd without a valid bearer — enforcement is
    structural, not per-handler."""
    assert auth_on.get("/api/tap/probe").status_code == 401
    assert auth_on.get("/api/tap/probe", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = auth_on.get("/api/tap/probe", headers={"Authorization": "Bearer " + TAP_TOKEN})
    assert ok.status_code == 200
    assert ok.json()["scheme"] == "tap"


def test_public_exact_routes_need_no_credential(auth_on: TestClient) -> None:
    r = auth_on.get("/health")
    assert r.status_code == 200
    assert r.json()["scheme"] == "public"


def test_basic_scheme_applies_to_everything_else(auth_on: TestClient) -> None:
    assert auth_on.get("/api/whatever").status_code == 401
    # A tap bearer is NOT accepted on a Basic route.
    assert auth_on.get("/api/whatever", headers={"Authorization": "Bearer " + TAP_TOKEN}).status_code == 401
    ok = auth_on.get("/api/whatever", auth=(_config.AUTH_USER, BASIC_PASS))
    assert ok.status_code == 200
    assert ok.json()["scheme"] == "basic"


def test_auth_disabled_passes_every_scheme_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    with TestClient(_mini_app()) as c:
        assert c.get("/api/tap/probe").status_code == 200
        assert c.get("/health").status_code == 200
        assert c.get("/api/whatever").status_code == 200
