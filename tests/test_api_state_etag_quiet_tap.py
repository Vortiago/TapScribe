"""RED contract for issue #217 — a quiet-but-OPEN tap must stop busting the
/api/state weak ETag every ~0.5 s.

The ETag hashes the whole compact `/api/state` body (`tapscribe/state_view.py`),
and each open
tap's `active` row embeds a raw `bytes_received` (incremented per 20 ms frame)
and a raw float `level` (per-frame volume meter). So while any tap is open,
every 2 Hz poll produces a fresh ETag and reships the ENTIRE state — the full
O(library) sessions catalog, the live-feed deque, whole prompt/hotwords file
contents, the people rows — even when nothing an operator can see changed.

The fix quantizes those two volatile scalars at the serialization boundary so a
sub-display-granularity jitter no longer changes the body: a quiet-but-open tap
then serves the same ETag and the poll 304s.

Test 1 pins the HARM at the real boundary — the actual `/api/state` ETag over
the real payload (no unit-in-isolation seam that could pass while the wiring is
wrong): a near-zero change to BOTH scalars must not change the ETag. It first
asserts the payload is stable at rest, because the whole harm claim rests on
those two scalars being the ONLY per-tick volatile fields. Test 2 is the
degenerate-fix guardrail: a large, operator-visible change must STILL change the
ETag, so "quantize" can't be implemented as "drop the fields / constant ETag".

The mutations are deliberately EXTREME (near-zero vs. multi-megabyte) so the
contract passes ANY admissible quantization granularity rather than pinning the
issue's suggested "2 decimals / 32 KB" numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial

import anyio.from_thread
import pytest
from fastapi.testclient import TestClient

from tapscribe.app import app, get_recorder
from tapscribe.recorder import ActiveStream, Recorder
from tapscribe.state_view import TAP_BYTES_BUCKET


@pytest.fixture
def client(recorder_under_test: Recorder):
    app.dependency_overrides[get_recorder] = lambda: recorder_under_test
    app.state.recorder = recorder_under_test
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _register_open_tap(
    recorder: Recorder, *, conn_id: str, identity: str, bytes_received: int, level: float
) -> None:
    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(
            recorder.streams.register,
            ActiveStream(
                conn_id=conn_id,
                identity=identity,
                name=identity,
                filename=f"{identity}.wav",
                started_at=datetime.now(UTC),
                bytes_received=bytes_received,
                level=level,
            ),
        )


def _update_tap(recorder: Recorder, conn_id: str, *, bytes_received: int, level: float) -> None:
    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(partial(recorder.streams.update_bytes, conn_id, bytes_received, level=level))


def _etag(client: TestClient) -> str:
    r = client.get("/api/state")
    assert r.status_code == 200
    return r.headers["etag"]


def _tap_row(client: TestClient, identity: str) -> dict:
    """The served /api/state `active` row for one tap — the value the operator
    actually sees, AFTER the display-granularity quantization. Matched by
    identity (not row order) so the pins don't depend on iteration order."""
    r = client.get("/api/state")
    assert r.status_code == 200
    rows = [row for row in r.json()["active"] if row["identity"] == identity]
    assert len(rows) == 1, f"expected exactly one active row for {identity!r}, got {len(rows)}"
    return rows[0]


def test_a_subgranularity_tap_update_does_not_bust_the_state_etag(
    client: TestClient, recorder_under_test: Recorder
):
    """A quiet-but-open tap whose bytes_received/level jitter below the display
    granularity must serve the SAME ETag, so the dashboard's poll 304s instead
    of reshipping the whole O(library) state every tick."""
    _register_open_tap(
        recorder_under_test, conn_id="c-quiet", identity="quiet-tap", bytes_received=64_000, level=0.50
    )

    # Warm-up GET settles first-appearance side effects (the People registry
    # auto-binds a brand-new live identity and saves people.json once), so the
    # payload is stable at rest and the comparison isolates the tap scalars.
    _etag(client)

    etag_before = _etag(client)
    # Precondition: with nothing changed the payload is byte-identical. The harm
    # claim rests on the two tap scalars being the ONLY volatile fields — if a
    # third changes per tick this fails loudly rather than hiding a RED-forever.
    assert _etag(client) == etag_before, (
        "/api/state is not stable at rest — a third per-tick volatile field exists"
    )

    # Sub-granularity jitter of BOTH scalars: one more 20 ms frame's worth of
    # bytes, a hair of level drift — below any sane display bucket.
    _update_tap(recorder_under_test, "c-quiet", bytes_received=64_001, level=0.5001)

    assert _etag(client) == etag_before, (
        "a sub-granularity tap update busted the /api/state ETag — quantize the "
        "volatile tap scalars (level, bytes_received) at the serialization boundary "
        "so a quiet-but-open tap stops reshipping the whole state at 2 Hz"
    )


def test_a_meaningful_tap_update_still_busts_the_state_etag(
    client: TestClient, recorder_under_test: Recorder
):
    """Degenerate-fix guardrail: quantization must not collapse to a constant. A
    large, operator-visible change (a megabyte of audio, a big level swing) must
    still change the ETag, or the live dashboard would freeze mid-meeting."""
    _register_open_tap(
        recorder_under_test, conn_id="c-loud", identity="loud-tap", bytes_received=64_000, level=0.05
    )
    _etag(client)  # warm-up

    etag_before = _etag(client)
    _update_tap(recorder_under_test, "c-loud", bytes_received=64_000 + 5_000_000, level=0.95)

    assert _etag(client) != etag_before, (
        "a large tap update did NOT change the ETag — the quantization is degenerate "
        "(constant), which would freeze the live dashboard during a meeting"
    )


def test_bytes_received_round_to_nearest_edges_are_served(client: TestClient, recorder_under_test: Recorder):
    """Pin the exact round-to-nearest-TAP_BYTES_BUCKET edges the operator sees on
    the served `active` row (not just one interior bucket): the whole first
    half-bucket collapses to 0 — a brand-new recording tap reads "0 B" until it
    has buffered half a bucket — and the half-bucket midpoint rounds UP to a full
    bucket (the 2x inflation). Both are #217-SANCTIONED cosmetic effects of the
    ETag-stability quantization (the frontend renders these same values), pinned
    here so a well-meaning "fix" back to raw counters is caught."""
    _register_open_tap(
        recorder_under_test, conn_id="c-edge", identity="edge-tap", bytes_received=1, level=0.5
    )
    # A single buffered byte is inside the first half-bucket → served as 0 ("0 B").
    assert _tap_row(client, "edge-tap")["bytes_received"] == 0

    # The half-bucket midpoint is the round-to-nearest tie → rounds UP to a full bucket.
    _update_tap(recorder_under_test, "c-edge", bytes_received=TAP_BYTES_BUCKET // 2, level=0.5)
    assert _tap_row(client, "edge-tap")["bytes_received"] == TAP_BYTES_BUCKET


def test_quantized_tap_scalars_keep_their_json_types(client: TestClient, recorder_under_test: Recorder):
    """The quantization must preserve types across the serialize boundary:
    bytes_received stays int, level stays float. A `//`→`/` typo would make
    bytes_received 65536.0, which serializes CONSISTENTLY every poll — so the
    ETag-stability tests would pass it straight through; only a type check on the
    served body catches it (and the frontend's byte formatting breaks on a float)."""
    _register_open_tap(
        recorder_under_test, conn_id="c-types", identity="types-tap", bytes_received=64_000, level=0.5
    )
    row = _tap_row(client, "types-tap")
    assert isinstance(row["bytes_received"], int), "bytes_received must stay int (guard //→/ )"
    assert isinstance(row["level"], float), "level must stay float"


def test_granularity_is_pinned_at_the_real_harm_rate(client: TestClient, recorder_under_test: Recorder):
    """Pin the bucket at BOTH ends via the production harm-rate, not the 1-byte
    proxy (which any bucket >=2 bytes passes): a realistic sub-bucket poll delta
    (a quarter bucket, ~= one poll-interval's worth of 20 ms frames) must HOLD the
    ETag, while advancing a whole bucket — an operator-visible jump — must BUST
    it. Deltas ride TAP_BYTES_BUCKET so this pins the round-to-nearest semantics
    through a deliberate bucket retune; the exact magnitudes live in the edge
    test above."""
    start = 64_000
    _register_open_tap(
        recorder_under_test, conn_id="c-rate", identity="rate-tap", bytes_received=start, level=0.5
    )
    _etag(client)  # warm-up — settle first-appearance People auto-bind

    etag_before = _etag(client)
    # A quarter-bucket advance stays inside the same bucket → same ETag.
    _update_tap(recorder_under_test, "c-rate", bytes_received=start + TAP_BYTES_BUCKET // 4, level=0.5)
    assert _etag(client) == etag_before, (
        "a sub-bucket (real poll-rate) advance busted the ETag — the display "
        "bucket is smaller than one poll-interval's worth of audio frames"
    )

    # A full-bucket advance crosses into the next bucket → operator-visible → ETag busts.
    _update_tap(recorder_under_test, "c-rate", bytes_received=start + TAP_BYTES_BUCKET, level=0.5)
    assert _etag(client) != etag_before, (
        "a full-bucket advance did NOT change the ETag — the display bucket is "
        "coarser than TAP_BYTES_BUCKET, hiding operator-visible growth"
    )
