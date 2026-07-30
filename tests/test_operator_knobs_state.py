"""RED contract for #210 — the knobs reach the DASHBOARD, over the real routes.

Companion to `test_operator_knobs_config.py`, which pins the resolver. This file
pins the two seams the dashboard actually talks to, driven end-to-end through the
FastAPI app rather than through a helper:

  * ``GET /api/state`` must SURFACE each knob's resolved value, so the Settings view
    can render the value in force. `/api/state` is the dashboard's whole model of the
    recorder; a knob the operator can write but not SEE is still an env-only knob in
    practice.
  * ``PUT /api/config/{key}`` must accept a valid write and REFUSE an invalid one
    with a 4xx — the route the Settings field saves through.

Why the round-trip is pinned at the route and not at `write_config`: the Python gate
and `tsc` are both blind to a client that saves to the wrong URL, and the resolver
tests would stay green with the route unwired. This is the wiring hole a unit-only
contract leaves open (the #217 lesson).

SUPERSEDES A PINNED SIBLING: `tests/test_state_view.py` holds `_PAYLOAD_KEYS` and
`test_the_payload_keys_are_the_wire_contract`, which pin the /api/state key list
EXACTLY. Adding keys makes that test fail by design — it must be UPDATED to include
the new ones (it is deliberately NOT protected for this slice). Keep the new keys as
top-level siblings of the landed `idle_ttl_s`, matching #347's precedent.
"""

from __future__ import annotations

import pytest
from conftest import recorder_under_test  # noqa: F401  # type: ignore[import-not-found]
from fastapi.testclient import TestClient

from tapscribe.app import app, get_recorder

# key on the wire (PUT /api/config/{key}) → key in the /api/state payload, plus a
# valid sample and an invalid one. Every knob is pinned; a sweep that wires only the
# headline knob ships the rest invisible to the dashboard.
KNOB_ROUTES = [
    ("parakeet-chunk-s", "parakeet_chunk_s", "300", 300.0, "abc"),
    ("parakeet-overlap-s", "parakeet_overlap_s", "20", 20.0, "abc"),
    ("summarize-timeout-s", "summarize_timeout_s", "600", 600.0, "abc"),
    ("summarize-gguf-ctx", "summarize_gguf_ctx", "16384", 16384, "abc"),
]
_IDS = [k[0] for k in KNOB_ROUTES]


@pytest.fixture
def client(recorder_under_test):  # noqa: F811
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.parametrize(("key", "state_key", "sample", "expected", "bad"), KNOB_ROUTES, ids=_IDS)
def test_api_state_surfaces_the_knob(client, key, state_key, sample, expected, bad) -> None:
    # DISCRIMINATOR (RED at base): the Settings view needs the value in force, so it
    # must be on the wire. Base has no such key.
    body = client.get("/api/state").json()
    assert state_key in body, (
        f"/api/state must surface {state_key} so the dashboard can render the value in force "
        f"(idle_ttl_s is the landed precedent)"
    )


@pytest.mark.parametrize(("key", "state_key", "sample", "expected", "bad"), KNOB_ROUTES, ids=_IDS)
def test_dashboard_write_round_trips_to_api_state(client, key, state_key, sample, expected, bad) -> None:
    # DISCRIMINATOR (RED at base): the FULL operator loop — save in Settings, see the
    # new value on the next poll — over the real routes. A resolver wired to a file
    # nothing writes, or a route that writes a file nothing reads, both fail here
    # while a unit-only contract stays green.
    r = client.put(f"/api/config/{key}", json={"content": sample})
    assert r.status_code == 200, f"PUT /api/config/{key} must accept a valid value, got {r.status_code}"
    body = client.get("/api/state").json()
    assert body[state_key] == expected, f"{key}: a dashboard write must be visible on the next /api/state"


@pytest.mark.parametrize(("key", "state_key", "sample", "expected", "bad"), KNOB_ROUTES, ids=_IDS)
def test_invalid_dashboard_write_is_refused_with_4xx(client, key, state_key, sample, expected, bad) -> None:
    # The write-time validator must surface as a CLIENT error, not a 500 — the
    # Settings field shows the message. A knob registered without a `check=` returns
    # 200 here and lands a garbage value on disk.
    r = client.put(f"/api/config/{key}", json={"content": bad})
    assert 400 <= r.status_code < 500, (
        f"PUT /api/config/{key} with an invalid value must be a 4xx, got {r.status_code}"
    )


@pytest.mark.parametrize(("key", "state_key", "sample", "expected", "bad"), KNOB_ROUTES, ids=_IDS)
def test_the_knob_participates_in_the_state_etag(client, key, state_key, sample, expected, bad) -> None:
    # ADVERSARIAL: /api/state is ETag-gated and the dashboard skips a 304 tick
    # entirely. A key that does not vary the ETag renders once and then goes stale
    # forever — the operator saves, and the field never updates.
    first = client.get("/api/state")
    etag = first.headers["etag"]
    client.put(f"/api/config/{key}", json={"content": sample})
    again = client.get("/api/state", headers={"If-None-Match": etag})
    assert again.status_code == 200, (
        f"{key}: changing the knob must change the /api/state ETag, or the dashboard 304s "
        "and never repaints the new value"
    )


def test_api_state_surfaces_the_specialist_table_read_only(client) -> None:
    # #210 asks for the specialist map to be VISIBLE ("visibility beats editability
    # here"). It is a launch-time knob, so it is surfaced and NOT writable.
    body = client.get("/api/state").json()
    assert "specialists" in body, "/api/state must surface the specialist language→model map"
    assert isinstance(body["specialists"], dict)
    r = client.put("/api/config/specialists", json={"content": "no=whisper"})
    assert r.status_code >= 400, "the specialist map is read-only in #210 — it must have no write route"


def test_idle_ttl_is_still_surfaced(client) -> None:
    # DO-NOT-TOUCH sibling, pinned positively: #347's key must survive the sweep that
    # adds its four neighbours.
    assert "idle_ttl_s" in client.get("/api/state").json()
