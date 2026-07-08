"""End-to-end tests for the /tap WebSocket — the merged Bridge contract.

We spin up a fake whisperlivekit-server in-process and point the Recorder
at it (via LiveConfig host/port). Then we open a /tap WS via TestClient,
send PCM bytes, and verify both the WAV is written AND the relay
forwarded bytes to the fake WlK AND settled-lines pushed by the fake
landed in recorder.transcripts.
"""

from __future__ import annotations

import re
import time
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import (
    FakeWlkThread,  # type: ignore[import-not-found]  # noqa: E402  # pytest puts tests/ on sys.path so `from conftest import` resolves the project's tests/conftest.py
    build_tap_recorder,
)
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.recorder import Recorder

# ---------------------------------------------------------------------------
# Fixtures (the `fake_wlk` fixture is shared via conftest.py so the
# TapFanOut unit tests can use the same WlK stand-in.)
# ---------------------------------------------------------------------------


@pytest.fixture
def recorder_with_fake_wlk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_wlk: FakeWlkThread
) -> Recorder:
    """Build a Recorder pointed at the fake WlK. Pretend the live channel
    is 'running' by tweaking LiveChannel.info — we don't actually spawn
    a subprocess, but the relay only needs the host/port to connect."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", False)
    monkeypatch.setattr(_config, "AUTO_START_LIVE", False)
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(_config, "CONFIG_DIR", tmp_path / "config")
    return build_tap_recorder(tmp_path, port=fake_wlk.port, gate_kind="backend", live_running=True)


@pytest.fixture
def client(recorder_with_fake_wlk: Recorder) -> Iterator[TestClient]:
    app.dependency_overrides[get_recorder] = lambda: recorder_with_fake_wlk
    app.state.recorder = recorder_with_fake_wlk
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def auth_client(recorder_with_fake_wlk: Recorder, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Like `client` but with AUTH_ENABLED, so the tap-token gates run: the
    /tap WS subprotocol check (TestTapAuth) and the /api/tap/new-session bearer
    check (TestTapNewSession)."""
    monkeypatch.setattr(_config, "AUTH_ENABLED", True)
    app.dependency_overrides[get_recorder] = lambda: recorder_with_fake_wlk
    app.state.recorder = recorder_with_fake_wlk
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tap-bearer scheme: structural sweep (ADR-0008)
#
# Enforcement of the tap bearer lives in the auth middleware, keyed on
# TAP_PREFIX, so EVERY registered route under it is gated by construction.
# This sweep discovers those routes from `app.routes`, so a future tap route
# is covered the moment it's registered — no per-route 401 test to remember.
# ---------------------------------------------------------------------------

_TAP_ROUTES = sorted(
    {
        (
            next(m for m in sorted(r.methods) if m not in {"HEAD", "OPTIONS"}),
            re.sub(r"\{[^}]+\}", "x", r.path),  # concrete path-param value
        )
        for r in app.routes
        if isinstance(r, APIRoute) and r.path.startswith(_config.TAP_PREFIX + "/")
    }
)


def test_tap_route_inventory_is_non_empty() -> None:
    # Guard the guard: if the filter matched nothing, the parametrised sweep
    # below would vacuously pass.
    assert _TAP_ROUTES, "expected at least the new-session + pipeline tap routes"


@pytest.mark.parametrize("method,path", _TAP_ROUTES)
def test_every_registered_tap_route_requires_bearer(auth_client: TestClient, method: str, path: str) -> None:
    """Every /api/tap/* route rejects a missing AND a wrong bearer, enforced
    by the auth middleware — so adding an un-gated tap route is impossible."""
    assert auth_client.request(method, path).status_code == 401
    assert (
        auth_client.request(
            method, path, headers={"Authorization": "Bearer definitely-not-the-token"}
        ).status_code
        == 401
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tap_endpoint_writes_wav_and_records_bytes_in_active_streams(
    client: TestClient,
    recorder_with_fake_wlk: Recorder,
    fake_wlk: FakeWlkThread,
):
    """Tracer bullet: open /tap, send a PCM frame, close. Assert WAV
    written + relay forwarded bytes."""
    pcm_frame = b"\x10\x00" * 320  # 20 ms at 16 kHz mono int16
    with client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
        ws.send_bytes(pcm_frame)
        ws.send_bytes(pcm_frame)
        # Wait for the relay to forward both frames to the fake server.
        assert _wait_for_relay_bytes(fake_wlk, len(pcm_frame) * 2), (
            "relay did not deliver both frames within timeout"
        )

    # WAV file landed in the session dir
    wavs = list(recorder_with_fake_wlk.session_dir.glob("*.wav"))
    assert len(wavs) == 1
    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 640  # two 320-sample frames

    # Relay forwarded bytes to the fake WlK
    received = b"".join(fake_wlk.received)
    assert pcm_frame * 2 in received


def test_tap_settled_lines_from_wlk_land_in_live_transcripts(
    client: TestClient,
    recorder_with_fake_wlk: Recorder,
    fake_wlk: FakeWlkThread,
):
    """When WlK pushes a settled line during a /tap WS, it should land in
    recorder.transcripts attributed to the WS's identity/name. WlK
    snapshots are cumulative and the relay holds the in-flight tail
    until either a newer line arrives or the relay closes — pushing a
    second line surfaces the first immediately."""
    pcm_frame = b"\x10\x00" * 320
    with client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
        ws.send_bytes(pcm_frame)
        assert _wait_for_relay_bytes(fake_wlk, len(pcm_frame))
        fake_wlk.push_committed("hello from WlK")
        fake_wlk.push_committed("second from WlK")
        # The first push superseded the tail position of "hello from WlK"
        # so it must reach transcripts before the WS closes.
        assert _wait_for_transcript_text(recorder_with_fake_wlk, "hello from WlK")

    # The WS handler's finally → relay.close() drain runs after exit;
    # wait for the tail line ("second from WlK") to surface before
    # snapshotting.
    assert _wait_for_transcript_text(recorder_with_fake_wlk, "second from WlK")
    snap = recorder_with_fake_wlk.transcripts.snapshot()
    texts = [e["text"] for e in snap]
    assert "hello from WlK" in texts
    assert "second from WlK" in texts
    for entry in snap:
        assert entry["identity"] == "alice"
        assert entry["name"] == "Alice"


def test_tap_settled_lines_surface_in_api_state_live_feed(
    client: TestClient,
    recorder_with_fake_wlk: Recorder,
    fake_wlk: FakeWlkThread,
):
    """End-to-end dashboard smoke test: the path the operator actually
    sees. /tap WS opens → fake WlK pushes settled lines → /api/state
    returns them under `live_feed` with the right attribution. This is
    what the dashboard's renderLiveFeed() consumes."""
    pcm_frame = b"\x10\x00" * 320
    with client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
        ws.send_bytes(pcm_frame)
        assert _wait_for_relay_bytes(fake_wlk, len(pcm_frame))
        fake_wlk.push_committed("first settled line")
        fake_wlk.push_committed("second settled line")
        assert _wait_for_transcript_text(recorder_with_fake_wlk, "first settled line")

    # The tail line lands after the WS handler's drain runs.
    assert _wait_for_transcript_text(recorder_with_fake_wlk, "second settled line")
    state = client.get("/api/state").json()
    feed = state["live_feed"]
    texts = [e["text"] for e in feed]
    assert "first settled line" in texts
    assert "second settled line" in texts
    # Attribution survives end-to-end.
    for e in feed:
        assert e["identity"] == "alice"
        assert e["name"] == "Alice"
        assert e["session"] == recorder_with_fake_wlk.session_start
        assert "ts" in e


def test_tap_drains_tail_caption_after_bridge_disconnects(
    client: TestClient,
    recorder_with_fake_wlk: Recorder,
    fake_wlk: FakeWlkThread,
):
    """Per Q2: settled lines emitted by WlK during the post-disconnect
    drain window must still land in transcripts."""
    pcm_frame = b"\x10\x00" * 320
    with client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
        ws.send_bytes(pcm_frame)
        assert _wait_for_relay_bytes(fake_wlk, len(pcm_frame))
        # Push the settled line as the WS context exits — the relay's
        # close() drain window must catch it.
        fake_wlk.push_committed("tail caption")

    # The relay's drain timeout flushes the in-flight tail; poll until
    # it lands rather than guessing a sleep duration.
    assert _wait_for_transcript_text(recorder_with_fake_wlk, "tail caption")


def test_tap_with_recording_paused_closes_immediately(
    client: TestClient,
    recorder_with_fake_wlk: Recorder,
):
    """The recording-toggle pause behaviour pre-existed on /record; it
    survives the rename to /tap. Bridge connects, gets accepted, gets
    closed cleanly with no WAV written."""
    from starlette.websockets import WebSocketDisconnect

    recorder_with_fake_wlk.recording_enabled = False
    with client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
        # Server closes immediately after accept; receive raises on close
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
    wavs = list(recorder_with_fake_wlk.session_dir.glob("*.wav"))
    assert wavs == []


def test_tap_without_wlk_running_still_writes_wav(
    client: TestClient,
    recorder_with_fake_wlk: Recorder,
):
    """Per Q2: silent graceful degradation. If LiveChannel isn't running,
    the relay isn't attempted; WAV writing proceeds normally."""
    # Mark the live channel as stopped
    recorder_with_fake_wlk.live._proc = None

    with client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
        ws.send_bytes(b"\x10\x00" * 320)

    wavs = list(recorder_with_fake_wlk.session_dir.glob("*.wav"))
    assert len(wavs) == 1


def test_old_record_route_no_longer_exists(
    client: TestClient,
    recorder_with_fake_wlk: Recorder,  # noqa: ARG001
):
    """The /record endpoint was renamed to /tap. The old name should 404
    so old bridges fail loudly rather than silently writing nothing."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/record?identity=alice&name=Alice"):
        pass


def test_old_live_transcript_post_route_is_gone(
    client: TestClient,
    recorder_with_fake_wlk: Recorder,  # noqa: ARG001
):
    """Per the architectural cleanup, the Bridge no longer POSTs settled
    lines back to the Recorder — those are consumed internally by the
    relay. The old POST route is removed; DELETE stays for the
    dashboard's clear-feed button."""
    r = client.post("/api/live-transcript", json={"text": "should not work"})
    assert r.status_code in (404, 405)
    # DELETE still works
    r2 = client.delete("/api/live-transcript")
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# Utterance resume: a /tap WS that reconnects with the same utterance_id
# appends to the same WAV instead of creating a second file.
# ---------------------------------------------------------------------------


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Generic polling helper. Replaces hand-rolled `time.sleep(...)` waits
    sprinkled through this module — those raced the relay's drain task
    and flaked on loaded CI runners. Use this anywhere a test needs to
    wait for state to settle, with the actual condition spelled out.
    Returns True if the predicate ever returned truthy within `timeout`,
    False if it timed out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _wait_for_utterance_closed(recorder: Recorder, utt: str, timeout: float = 5.0) -> bool:
    """Poll until the server-side finally has marked the utterance closed
    in the index. TestClient's websocket_connect context manager doesn't
    await the server handler's finally block; without this, the resume
    path races against the prior release()."""

    def closed() -> bool:
        rec = recorder.utterances.snapshot().get(utt)
        return rec is not None and not rec.open

    return _wait_until(closed, timeout=timeout)


def _wait_for_transcript_text(recorder: Recorder, text: str, timeout: float = 5.0) -> bool:
    """Poll until `text` appears in the live transcripts feed. Replaces the
    pattern of `sleep(0.2); assert text in snapshot` that flakes when the
    relay's consumer task hasn't yet awoken to read the WlK push."""
    return _wait_until(
        lambda: any(e["text"] == text for e in recorder.transcripts.snapshot()),
        timeout=timeout,
    )


def _wait_for_relay_bytes(fake_wlk: FakeWlkThread, minimum: int, timeout: float = 5.0) -> bool:
    """Poll until the fake WlK has buffered at least `minimum` bytes from
    the relay. Replaces the `sleep(0.15)` after a send_bytes which was
    racing the WebSocket task scheduler."""
    return _wait_until(
        lambda: sum(len(b) for b in fake_wlk.received) >= minimum,
        timeout=timeout,
    )


def test_tap_resume_with_same_utterance_id_appends_to_same_wav(
    client: TestClient,
    recorder_with_fake_wlk: Recorder,  # noqa: ARG001
):
    """Mid-utterance blip recovery: bridge passes a stable utterance_id;
    second /tap with the same id appends rather than starting a new file."""
    # Disable the WlK relay path — it can hold the event loop in the
    # test environment long enough to bust our deterministic polling. The
    # resume behaviour we're verifying is WAV-side only.
    recorder_with_fake_wlk.live._proc = None
    pcm_frame = b"\x10\x00" * 320  # 20 ms frame
    utt = "abc123def456"

    # First half of the utterance
    with client.websocket_connect(
        f"/tap?identity=alice&name=Alice&utterance_id={utt}",
    ) as ws:
        ws.send_bytes(pcm_frame)
        ws.send_bytes(pcm_frame)
    assert _wait_for_utterance_closed(recorder_with_fake_wlk, utt)

    # Reconnect with same id — should resume the existing WAV
    with client.websocket_connect(
        f"/tap?identity=alice&name=Alice&utterance_id={utt}",
    ) as ws:
        ws.send_bytes(pcm_frame)
    # Wait again so the WAV has been finalised before we open it for reading.
    assert _wait_for_utterance_closed(recorder_with_fake_wlk, utt)

    wavs = list(recorder_with_fake_wlk.session_dir.glob("*.wav"))
    assert len(wavs) == 1, f"expected one WAV, got {[w.name for w in wavs]}"
    with wave.open(str(wavs[0]), "rb") as w:
        # 3 frames * 320 samples = 960 frames if append worked
        assert w.getnframes() == 960


def test_tap_resume_rejected_for_different_identity(
    client: TestClient,
    recorder_with_fake_wlk: Recorder,
):
    """A reused utterance_id with a different identity must not collide —
    it gets a fresh WAV."""
    # Disable the WlK relay path for the same reason as the resume test
    # above: its `await candidate.connect()` is a slow yield that the
    # TestClient's portal-shutdown cancellation can interrupt before the
    # handler reads the queued PCM frame, leaving bytes_received=0 and
    # the empty-WAV cleanup branch deletes the file under our feet.
    recorder_with_fake_wlk.live._proc = None
    pcm_frame = b"\x10\x00" * 320
    utt = "shared-id-xyz"

    with client.websocket_connect(
        f"/tap?identity=alice&name=Alice&utterance_id={utt}",
    ) as ws:
        ws.send_bytes(pcm_frame)
    assert _wait_for_utterance_closed(recorder_with_fake_wlk, utt)

    with client.websocket_connect(
        f"/tap?identity=bob&name=Bob&utterance_id={utt}",
    ) as ws:
        ws.send_bytes(pcm_frame)
    assert _wait_for_utterance_closed(recorder_with_fake_wlk, utt)

    wavs = sorted(recorder_with_fake_wlk.session_dir.glob("*.wav"))
    assert len(wavs) == 2


def test_tap_distinct_utterance_ids_produce_distinct_wavs(
    client: TestClient,
    recorder_with_fake_wlk: Recorder,
):
    """Sanity: different utterance_ids (the bridge's normal between-mute
    behaviour) still produce one WAV per id."""
    # See test_tap_resume_rejected_for_different_identity for why we
    # bypass the relay here — same TestClient cancellation race.
    recorder_with_fake_wlk.live._proc = None
    pcm_frame = b"\x10\x00" * 320

    with client.websocket_connect(
        "/tap?identity=alice&name=Alice&utterance_id=first",
    ) as ws:
        ws.send_bytes(pcm_frame)
    assert _wait_for_utterance_closed(recorder_with_fake_wlk, "first")

    with client.websocket_connect(
        "/tap?identity=alice&name=Alice&utterance_id=second",
    ) as ws:
        ws.send_bytes(pcm_frame)
    assert _wait_for_utterance_closed(recorder_with_fake_wlk, "second")

    wavs = list(recorder_with_fake_wlk.session_dir.glob("*.wav"))
    assert len(wavs) == 2


def test_tap_resume_past_window_starts_fresh_wav(
    client: TestClient,
    recorder_with_fake_wlk: Recorder,
    monkeypatch: pytest.MonkeyPatch,
):
    """If the bridge reconnects long after the resume window has elapsed,
    we treat it as a new utterance and write a new WAV."""
    recorder_with_fake_wlk.live._proc = None
    pcm_frame = b"\x10\x00" * 320
    utt = "expiring-utt"

    # Shrink the resume window so the test doesn't sleep for a minute.
    monkeypatch.setattr(
        recorder_with_fake_wlk.utterances,
        "RESUME_WINDOW_SECONDS",
        0.05,
    )

    with client.websocket_connect(
        f"/tap?identity=alice&name=Alice&utterance_id={utt}",
    ) as ws:
        ws.send_bytes(pcm_frame)
    assert _wait_for_utterance_closed(recorder_with_fake_wlk, utt)
    # Deliberate sleep, not a race-hiding one: the test condition IS that
    # the resume window has elapsed, and there's no observable event to
    # poll on for "the window has aged past 0.05 s wall-clock." 5× the
    # window so we don't race the ~100-ms-resolution clock on Windows
    # under CI load — the matrix runs three OS × four Python versions
    # in parallel so the runner can easily slip past a tighter margin.
    time.sleep(0.25)

    with client.websocket_connect(
        f"/tap?identity=alice&name=Alice&utterance_id={utt}",
    ) as ws:
        ws.send_bytes(pcm_frame)
    assert _wait_for_utterance_closed(recorder_with_fake_wlk, utt)

    wavs = list(recorder_with_fake_wlk.session_dir.glob("*.wav"))
    assert len(wavs) == 2


def test_tap_drain_then_reconnect_appends_tail_pcm_to_same_wav(
    client: TestClient,
    recorder_with_fake_wlk: Recorder,
):
    """Drain → append-tail end-to-end. Simulates the bridge buffering PCM
    during a network blip, then reconnecting with the same utterance_id
    and flushing the buffered audio. The WAV must end with N+M frames
    byte-for-byte identical to the concatenation of what was sent.

    This is the Python-side counterpart to the JS drain.test.js suite:
    those tests assert the bridge's *state machine* enters drain, holds
    the buffer, and flushes on reconnect. THIS test asserts that the
    Recorder side actually ends up with one WAV containing both halves
    — the invariant "one utterance = one WAV" only holds if both sides
    agree."""
    recorder_with_fake_wlk.live._proc = None  # relay-free, deterministic
    utt = "drain-utt-1"
    n_before = 4
    m_after_drain = 3
    # Deterministic per-frame contents so we can byte-compare.
    head_frames = [bytes([i % 256] * 2) * 320 for i in range(n_before)]
    tail_frames = [bytes([(i + 100) % 256] * 2) * 320 for i in range(m_after_drain)]

    # Pre-drain: bridge connects, streams head_frames, link blips closed.
    with client.websocket_connect(
        f"/tap?identity=alice&name=Alice&utterance_id={utt}",
    ) as ws:
        for f in head_frames:
            ws.send_bytes(f)
    assert _wait_for_utterance_closed(recorder_with_fake_wlk, utt)

    # Drain reconnect: same utterance_id, flushes the buffered tail.
    with client.websocket_connect(
        f"/tap?identity=alice&name=Alice&utterance_id={utt}",
    ) as ws:
        for f in tail_frames:
            ws.send_bytes(f)
    assert _wait_for_utterance_closed(recorder_with_fake_wlk, utt)

    wavs = list(recorder_with_fake_wlk.session_dir.glob("*.wav"))
    assert len(wavs) == 1, f"expected one WAV after drain-reconnect, got {[w.name for w in wavs]}"
    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        actual = w.readframes(w.getnframes())
    expected = b"".join(head_frames) + b"".join(tail_frames)
    assert actual == expected, "drained tail PCM was not appended byte-for-byte"


# ---------------------------------------------------------------------------
# /tap auth gate — Sec-WebSocket-Protocol bearer token
# ---------------------------------------------------------------------------


class TestTapAuth:
    """When AUTH_ENABLED, the /tap WS requires a Sec-WebSocket-Protocol of
    the form 'tapscribe.v1.tap.<token>'. With the right token the upgrade
    succeeds and the server echoes the same subprotocol back; with the
    wrong token (or none) the recorder closes the upgrade with code 4401."""

    def test_tap_accepts_correct_token_and_writes_wav(
        self,
        auth_client: TestClient,
        recorder_with_fake_wlk: Recorder,
    ):
        # The relay handshake to the fake WlK can race the test-client
        # close; match the existing /tap tests by side-stepping the relay
        # for this auth check — the focus here is the upgrade gate, not
        # the relay.
        recorder_with_fake_wlk.live._proc = None
        token = recorder_with_fake_wlk.tap.value
        from tapscribe.auth import TAP_SUBPROTOCOL_PREFIX

        subprotocol = TAP_SUBPROTOCOL_PREFIX + token
        with auth_client.websocket_connect(
            "/tap?identity=alice&name=Alice",
            subprotocols=[subprotocol],
        ) as ws:
            ws.send_bytes(b"\x10\x00" * 320)
        wavs = list(recorder_with_fake_wlk.session_dir.glob("*.wav"))
        assert len(wavs) == 1

    def test_tap_rejects_missing_subprotocol(
        self,
        auth_client: TestClient,
        recorder_with_fake_wlk: Recorder,
    ):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with auth_client.websocket_connect("/tap?identity=alice&name=Alice"):
                pass
        assert list(recorder_with_fake_wlk.session_dir.glob("*.wav")) == []

    def test_tap_rejects_wrong_token(
        self,
        auth_client: TestClient,
        recorder_with_fake_wlk: Recorder,
    ):
        from starlette.websockets import WebSocketDisconnect

        from tapscribe.auth import TAP_SUBPROTOCOL_PREFIX

        bad = TAP_SUBPROTOCOL_PREFIX + "definitely-not-the-token"
        with pytest.raises(WebSocketDisconnect):
            with auth_client.websocket_connect(
                "/tap?identity=alice&name=Alice",
                subprotocols=[bad],
            ):
                pass
        assert list(recorder_with_fake_wlk.session_dir.glob("*.wav")) == []

    def test_tap_rejects_unrelated_subprotocol(
        self,
        auth_client: TestClient,
        recorder_with_fake_wlk: Recorder,
    ):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with auth_client.websocket_connect(
                "/tap?identity=alice&name=Alice",
                subprotocols=["chat.v3"],
            ):
                pass
        assert list(recorder_with_fake_wlk.session_dir.glob("*.wav")) == []


class TestTapNewSession:
    """POST /api/tap/new-session is the bridge's HTTP control verb: rotate the
    session (and prune empties), authenticated by the tap token as a bearer
    header rather than dashboard Basic auth. The auth middleware's TAP-BEARER
    scheme (config.TAP_PREFIX) is the gate; the handler carries none (ADR-0008)."""

    @staticmethod
    def _touch_wav(recorder: Recorder) -> None:
        # The empty-current guard only checks for *.wav presence (not
        # contents), so a placeholder file is enough to make rotation fire.
        recorder.session_dir.mkdir(parents=True, exist_ok=True)
        (recorder.session_dir / "seed.wav").write_bytes(b"")

    def test_accepts_valid_bearer_token(self, auth_client: TestClient, recorder_with_fake_wlk: Recorder):
        self._touch_wav(recorder_with_fake_wlk)
        prev = recorder_with_fake_wlk.session_start
        token = recorder_with_fake_wlk.tap.value
        r = auth_client.post("/api/tap/new-session", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["rotated"] is True
        # _utc_session_id() has 1s resolution; a same-second rotation reuses
        # the id, so assert on the rotated flag + previous, not on a changed id.
        assert body["previous"] == prev

    def test_rejects_missing_token(self, auth_client: TestClient, recorder_with_fake_wlk: Recorder):
        self._touch_wav(recorder_with_fake_wlk)
        prev = recorder_with_fake_wlk.session_start
        r = auth_client.post("/api/tap/new-session")
        assert r.status_code == 401
        assert recorder_with_fake_wlk.session_start == prev  # not rotated

    def test_rejects_wrong_token(self, auth_client: TestClient, recorder_with_fake_wlk: Recorder):
        self._touch_wav(recorder_with_fake_wlk)
        prev = recorder_with_fake_wlk.session_start
        r = auth_client.post(
            "/api/tap/new-session",
            headers={"Authorization": "Bearer definitely-not-the-token"},
        )
        assert r.status_code == 401
        assert recorder_with_fake_wlk.session_start == prev


class TestDetachedNewSession:
    """POST /api/tap/new-session with {"detached": true} mints a fresh,
    isolated session for the calling Bridge: the directory exists on disk
    immediately and the global current session is left untouched. The
    legacy no-body call keeps its rotate semantics (TestTapNewSession)."""

    def test_detached_creates_fresh_session_without_rotating_global(
        self, client: TestClient, recorder_with_fake_wlk: Recorder
    ):
        global_before = recorder_with_fake_wlk.session_start
        r = client.post("/api/tap/new-session", json={"detached": True})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["detached"] is True
        detached_id = body["session"]
        # A fresh id, never the global current one — even when minted in
        # the same second as the global session's 1s-resolution id.
        assert detached_id and detached_id != global_before
        # The global current session is untouched (no rotation).
        assert recorder_with_fake_wlk.session_start == global_before
        # The directory materialises immediately so ?session= taps can
        # resolve it.
        assert (recorder_with_fake_wlk.recordings_dir / detached_id).is_dir()

    def test_detached_requires_the_tap_bearer_token_when_auth_enabled(
        self, auth_client: TestClient, recorder_with_fake_wlk: Recorder
    ):
        """The detached mode sits behind the same tap-token gate as the
        legacy rotate: no token → 401 and nothing created; valid token →
        the detached session is minted."""
        before = set(recorder_with_fake_wlk.recordings_dir.iterdir())
        r = auth_client.post("/api/tap/new-session", json={"detached": True})
        assert r.status_code == 401
        assert set(recorder_with_fake_wlk.recordings_dir.iterdir()) == before

        token = recorder_with_fake_wlk.tap.value
        r = auth_client.post(
            "/api/tap/new-session",
            json={"detached": True},
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 200 and r.json()["detached"] is True

    def test_malformed_body_is_rejected_not_silently_rotated(
        self, client: TestClient, recorder_with_fake_wlk: Recorder
    ):
        """A body that fails to parse must not fall through to the legacy
        rotate — the caller plausibly meant {"detached": true}, and a
        silent rotation would move the GLOBAL session out from under
        every plain tap."""
        TestTapNewSession._touch_wav(recorder_with_fake_wlk)
        before = recorder_with_fake_wlk.session_start
        dirs_before = set(recorder_with_fake_wlk.recordings_dir.iterdir())
        r = client.post(
            "/api/tap/new-session",
            content=b'{"detached": tru',
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400
        assert recorder_with_fake_wlk.session_start == before
        assert set(recorder_with_fake_wlk.recordings_dir.iterdir()) == dirs_before

    def test_detached_false_keeps_legacy_rotate_semantics(
        self, client: TestClient, recorder_with_fake_wlk: Recorder
    ):
        """An explicit {"detached": false} (and any body without the flag)
        is the legacy rotate, byte-for-byte response shape included."""
        TestTapNewSession._touch_wav(recorder_with_fake_wlk)
        before = recorder_with_fake_wlk.session_start
        r = client.post("/api/tap/new-session", json={"detached": False})
        assert r.status_code == 200
        body = r.json()
        assert body["rotated"] is True
        assert body["previous"] == before
        assert "detached" not in body

    def test_rotation_never_aliases_an_existing_detached_session(
        self, client: TestClient, recorder_with_fake_wlk: Recorder, monkeypatch: pytest.MonkeyPatch
    ):
        """The inverse of detached de-collision: a rotation minted in the
        same second as a detached session must NOT re-use the detached id —
        that would point the global current session at the detached dir and
        silently merge the two meetings."""
        monkeypatch.setattr("tapscribe.recorder._utc_session_id", lambda: "2099-01-01T00-00-00Z")
        detached_id = client.post("/api/tap/new-session", json={"detached": True}).json()["session"]
        assert detached_id == "2099-01-01T00-00-00Z"
        # Seed a WAV so the rotate's empty-session idempotency guard fires.
        recorder_with_fake_wlk.session_dir.mkdir(parents=True, exist_ok=True)
        (recorder_with_fake_wlk.session_dir / "seed.wav").write_bytes(b"")
        r = client.post("/api/tap/new-session")
        assert r.json()["rotated"] is True
        assert r.json()["current"] != detached_id
        assert recorder_with_fake_wlk.session_dir != recorder_with_fake_wlk.recordings_dir / detached_id

    def test_two_detached_creates_in_one_second_mint_distinct_sessions(
        self, client: TestClient, recorder_with_fake_wlk: Recorder, monkeypatch: pytest.MonkeyPatch
    ):
        """Deterministic de-collision: with the clock pinned, back-to-back
        detached creates still mint distinct, existing directories."""
        monkeypatch.setattr("tapscribe.recorder._utc_session_id", lambda: "2099-01-01T00-00-00Z")
        first = client.post("/api/tap/new-session", json={"detached": True}).json()["session"]
        second = client.post("/api/tap/new-session", json={"detached": True}).json()["session"]
        assert first != second
        assert (recorder_with_fake_wlk.recordings_dir / first).is_dir()
        assert (recorder_with_fake_wlk.recordings_dir / second).is_dir()

    def test_detached_session_is_an_ordinary_session_in_the_listing(
        self, client: TestClient, recorder_with_fake_wlk: Recorder
    ):
        """Bridge-created detached sessions are ordinary sessions: they
        appear in the dashboard listing (not flagged current) with their
        recorded WAVs, like any other session on disk."""
        recorder_with_fake_wlk.live._proc = None
        detached_id = client.post("/api/tap/new-session", json={"detached": True}).json()["session"]
        with client.websocket_connect(f"/tap?identity=alice&name=Alice&session={detached_id}") as ws:
            ws.send_bytes(b"\x10\x00" * 320)
        listing = {s["session"]: s for s in client.get("/sessions").json()}
        assert detached_id in listing
        assert listing[detached_id]["is_current"] is False
        assert listing[detached_id]["wav_count"] == 1


class TestTapPipeline:
    """POST/GET /api/tap/sessions/{session}/pipeline — the end-of-meeting
    pipeline's trigger and poll verbs. Tap-bearer authenticated like
    /api/tap/new-session by the auth middleware's TAP-BEARER scheme
    (config.TAP_PREFIX); the handlers carry no gate (ADR-0008). The trigger is fire-and-forget
    (202) and accepts NO model/summarizer/prompt fields; the poll reports
    stage progress while running, the persisted summary when done, and the
    failing stage's error when failed."""

    @staticmethod
    def _seed_session(recorder: Recorder, name: str = "meet1") -> Path:
        sd = recorder.recordings_dir / name
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "2026-01-01T01-00-00Z__alice__abc.wav").write_bytes(b"")
        return sd

    @staticmethod
    def _claim(recorder: Recorder, kind: str = "transcribe", **fields) -> None:
        from datetime import UTC, datetime

        import anyio.from_thread

        from tapscribe.recorder import JobState

        with anyio.from_thread.start_blocking_portal() as portal:
            claimed = portal.call(
                recorder.jobs.claim,
                JobState(
                    session="meet1",
                    kind=kind,  # type: ignore[arg-type]
                    current=0,
                    total=1,
                    started_at=datetime.now(UTC),
                    status="running",
                    **fields,
                ),
            )
            assert claimed

    def test_trigger_accepts_valid_bearer_and_returns_202_running(
        self, auth_client: TestClient, recorder_with_fake_wlk: Recorder
    ):
        self._seed_session(recorder_with_fake_wlk)
        token = recorder_with_fake_wlk.tap.value
        r = auth_client.post("/api/tap/sessions/meet1/pipeline", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["session"] == "meet1"
        assert body["state"] == "running"

    @pytest.mark.parametrize("verb", ["post", "get"])
    def test_both_verbs_reject_missing_and_wrong_bearer(
        self, auth_client: TestClient, recorder_with_fake_wlk: Recorder, verb: str
    ):
        """The /api/tap/ Basic-auth exemption widens ONLY to routes that
        carry their own bearer gate — both verbs must enforce it."""
        self._seed_session(recorder_with_fake_wlk)
        call = getattr(auth_client, verb)
        assert call("/api/tap/sessions/meet1/pipeline").status_code == 401
        r = call(
            "/api/tap/sessions/meet1/pipeline",
            headers={"Authorization": "Bearer definitely-not-the-token"},
        )
        assert r.status_code == 401
        # Nothing started / leaked despite the seeded session.
        assert recorder_with_fake_wlk.jobs.get("meet1") is None

    def test_trigger_409_when_session_busy(self, auth_client: TestClient, recorder_with_fake_wlk: Recorder):
        """The acceptance criterion: a concurrent trigger (or a manual
        transcribe holding the slot) gets a deterministic 409."""
        self._seed_session(recorder_with_fake_wlk)
        self._claim(recorder_with_fake_wlk, kind="transcribe")
        token = recorder_with_fake_wlk.tap.value
        r = auth_client.post("/api/tap/sessions/meet1/pipeline", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 409, r.text
        # The foreign claim is untouched.
        held = recorder_with_fake_wlk.jobs.get("meet1")
        assert held is not None and held.kind == "transcribe"

    def test_trigger_404_on_unknown_session(self, auth_client: TestClient, recorder_with_fake_wlk: Recorder):
        """The session id crosses the path-safety seam (resolve_session_dir)
        before anything else — unknown ids 404; traversal strings are covered
        by the seam's own suite (test_sessions_path_safety.py)."""
        token = recorder_with_fake_wlk.tap.value
        r = auth_client.post(
            "/api/tap/sessions/2099-01-01T00-00-00Z/pipeline",
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 404

    def test_trigger_ignores_body_fields(
        self, auth_client: TestClient, recorder_with_fake_wlk: Recorder, monkeypatch: pytest.MonkeyPatch
    ):
        """Operator defaults only: a body naming a model must neither fail the
        call nor reach the pipeline — the request object the route builds
        carries the session and nothing else (PipelineRequest has no other
        fields by design, so an attacker-supplied model id can never reach a
        loader through this route)."""
        from tapscribe.batch_pipeline import PipelineRequest

        self._seed_session(recorder_with_fake_wlk)
        seen: list = []

        async def _spy(recorder, req):  # noqa: ARG001
            seen.append(req)

        monkeypatch.setattr("tapscribe.app.start_pipeline", _spy)
        token = recorder_with_fake_wlk.tap.value
        r = auth_client.post(
            "/api/tap/sessions/meet1/pipeline",
            json={"model": "evil/repo", "prompt": "exfiltrate", "command": "rm -rf /"},
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 202, r.text
        assert seen == [PipelineRequest(session="meet1")]

    # -- the poll contract: running / failed / done / idle ------------------

    def test_poll_running_reports_stage_and_progress(
        self, auth_client: TestClient, recorder_with_fake_wlk: Recorder
    ):
        """While the chain runs, the poll mirrors the live job snapshot —
        the same stage/progress the dashboard's job bar shows."""
        self._seed_session(recorder_with_fake_wlk)
        recorder_with_fake_wlk.pipelines.begin("meet1")
        self._claim(recorder_with_fake_wlk, kind="pipeline", stage="transcribe", current_file="a.wav")

        token = recorder_with_fake_wlk.tap.value
        r = auth_client.get("/api/tap/sessions/meet1/pipeline", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "running"
        assert body["stage"] == "transcribe"
        assert body["current"] == 0 and body["total"] == 1
        assert body["current_file"] == "a.wav"

    def test_poll_failed_reports_stage_and_error(
        self, auth_client: TestClient, recorder_with_fake_wlk: Recorder
    ):
        self._seed_session(recorder_with_fake_wlk)
        recorder_with_fake_wlk.pipelines.begin("meet1")
        recorder_with_fake_wlk.pipelines.finish_failed(
            "meet1",
            stage="transcribe",
            error="no usable WAVs after stripping — no speech detected in this session",
            error_kind="NoUsableWavs",
        )

        token = recorder_with_fake_wlk.tap.value
        r = auth_client.get("/api/tap/sessions/meet1/pipeline", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "failed"
        assert body["stage"] == "transcribe"
        assert "no usable WAVs" in body["error"]
        assert body["error_kind"] == "NoUsableWavs"

    def test_poll_done_returns_persisted_summary(
        self, auth_client: TestClient, recorder_with_fake_wlk: Recorder
    ):
        """When the chain finished, the poll carries the SAME persisted
        summary the dashboard's summary endpoint serves (user story 30)."""
        from tapscribe.sessions import write_session_summary

        self._seed_session(recorder_with_fake_wlk)
        write_session_summary(
            "meet1",
            {"summary": "decided to ship", "source": "local", "summarized_at": "2026-06-10T12:00:00+00:00"},
        )
        recorder_with_fake_wlk.pipelines.begin("meet1")
        recorder_with_fake_wlk.pipelines.finish_done("meet1")

        token = recorder_with_fake_wlk.tap.value
        r = auth_client.get("/api/tap/sessions/meet1/pipeline", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "done"
        assert body["summary"]["summary"] == "decided to ship"

    def test_poll_done_when_only_the_persisted_summary_survives_a_restart(
        self, auth_client: TestClient, recorder_with_fake_wlk: Recorder
    ):
        """`recorder.pipelines` is in-memory only — after a Recorder restart
        a re-polling Bridge must still get its summary, answered from
        session-summary.json on disk."""
        from tapscribe.sessions import write_session_summary

        self._seed_session(recorder_with_fake_wlk)
        write_session_summary("meet1", {"summary": "survived the restart", "source": "local"})
        assert recorder_with_fake_wlk.pipelines.get("meet1") is None  # no record, as after boot

        token = recorder_with_fake_wlk.tap.value
        r = auth_client.get("/api/tap/sessions/meet1/pipeline", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "done"
        assert body["summary"]["summary"] == "survived the restart"

    def test_poll_idle_when_no_pipeline_ever_ran(
        self, auth_client: TestClient, recorder_with_fake_wlk: Recorder
    ):
        self._seed_session(recorder_with_fake_wlk)
        token = recorder_with_fake_wlk.tap.value
        r = auth_client.get("/api/tap/sessions/meet1/pipeline", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 200
        assert r.json()["state"] == "idle"


class TestTapSessionParam:
    """/tap?session=<id> directs a tap's WAV into that session instead of
    the global current one — the per-Bridge isolation of issue #100."""

    def test_session_param_isolates_tap_from_concurrent_global_tap(
        self, client: TestClient, recorder_with_fake_wlk: Recorder
    ):
        """Two concurrent taps against one Recorder: the one carrying
        ?session=<detached id> writes its WAV there; the plain one keeps
        writing to the global current session."""
        recorder_with_fake_wlk.live._proc = None  # WAV-routing focus, no relay
        detached_id = client.post("/api/tap/new-session", json={"detached": True}).json()["session"]
        pcm = b"\x10\x00" * 320
        with (
            client.websocket_connect(f"/tap?identity=alice&name=Alice&session={detached_id}") as ws_a,
            client.websocket_connect("/tap?identity=bob&name=Bob") as ws_b,
        ):
            ws_a.send_bytes(pcm)
            ws_b.send_bytes(pcm)

        detached_wavs = list((recorder_with_fake_wlk.recordings_dir / detached_id).glob("*.wav"))
        global_wavs = list(recorder_with_fake_wlk.session_dir.glob("*.wav"))
        assert len(detached_wavs) == 1 and "Alice" in detached_wavs[0].name
        assert len(global_wavs) == 1 and "Bob" in global_wavs[0].name

    @pytest.mark.parametrize(
        "bad_session",
        [
            "2099-01-01T00-00-00Z",  # well-formed but no such session on disk
            "..",  # traversal — rejected by the path-safety seam
            "",  # empty ?session= is invalid, not a fallback to the global
        ],
    )
    def test_unknown_or_invalid_session_refuses_the_upgrade(
        self, client: TestClient, recorder_with_fake_wlk: Recorder, bad_session: str
    ):
        """An unknown or invalid ?session= refuses the WS upgrade outright
        (mirroring token rejection) — a misconfigured bridge fails loudly
        instead of silently recording into the wrong session. The id
        crosses resolve_session_dir (the canonical path-safety seam), whose
        HTTPException(404) denies the upgrade with an HTTP 404 response
        before accept."""
        from starlette.testclient import WebSocketDenialResponse

        with pytest.raises(WebSocketDenialResponse) as exc_info:
            with client.websocket_connect(f"/tap?identity=alice&name=Alice&session={bad_session}"):
                pass
        assert exc_info.value.status_code == 404
        # Nothing was recorded anywhere.
        assert list(recorder_with_fake_wlk.recordings_dir.rglob("*.wav")) == []

    def test_open_tap_keeps_its_session_across_rotation(
        self, client: TestClient, recorder_with_fake_wlk: Recorder, monkeypatch: pytest.MonkeyPatch
    ):
        """Session affiliation snapshots at WS open: a rotation
        mid-utterance never re-homes the open tap — frames sent after the
        rotation keep landing in the WAV in the original session folder."""
        recorder_with_fake_wlk.live._proc = None
        original_dir = recorder_with_fake_wlk.session_dir
        # Session ids have 1s resolution, so a same-second rotation would
        # re-mint the SAME id and the two dirs couldn't be told apart. Pin
        # the clock boundary so the rotation lands on a distinct id.
        monkeypatch.setattr("tapscribe.recorder._utc_session_id", lambda: "2099-01-01T00-00-00Z")
        pcm = b"\x10\x00" * 320
        with client.websocket_connect("/tap?identity=alice&name=Alice") as ws:
            ws.send_bytes(pcm)
            # The WAV materialises during the open; wait for it so the
            # rotate endpoint's empty-session idempotency guard sees a
            # non-empty current session and actually rotates.
            deadline = time.time() + 5
            while not list(original_dir.glob("*.wav")) and time.time() < deadline:
                time.sleep(0.01)
            assert list(original_dir.glob("*.wav")), "tap WAV never materialised before rotation"
            assert client.post("/api/tap/new-session").json()["rotated"] is True
            ws.send_bytes(pcm)

        assert recorder_with_fake_wlk.session_dir != original_dir
        wavs = list(original_dir.glob("*.wav"))
        assert len(wavs) == 1
        with wave.open(str(wavs[0]), "rb") as w:
            assert w.getnframes() == 640  # both frames: pre- AND post-rotation
        # The new current session received nothing from the open tap.
        assert list(recorder_with_fake_wlk.session_dir.glob("*.wav")) == []

    def test_resume_with_same_utterance_id_appends_within_detached_session(
        self, client: TestClient, recorder_with_fake_wlk: Recorder
    ):
        """Blip recovery composes with ?session=: a reconnect carrying the
        same utterance_id AND the same detached session id resumes the
        existing WAV there — the resume seam matches on the tap's
        snapshotted session dir, not the global one."""
        recorder_with_fake_wlk.live._proc = None  # WAV-side focus, as in the global resume test
        detached_id = client.post("/api/tap/new-session", json={"detached": True}).json()["session"]
        pcm = b"\x10\x00" * 320
        utt = "feedbeef1234"

        with client.websocket_connect(
            f"/tap?identity=alice&name=Alice&utterance_id={utt}&session={detached_id}"
        ) as ws:
            ws.send_bytes(pcm)
            ws.send_bytes(pcm)
        assert _wait_for_utterance_closed(recorder_with_fake_wlk, utt)

        with client.websocket_connect(
            f"/tap?identity=alice&name=Alice&utterance_id={utt}&session={detached_id}"
        ) as ws:
            ws.send_bytes(pcm)
        assert _wait_for_utterance_closed(recorder_with_fake_wlk, utt)

        wavs = list((recorder_with_fake_wlk.recordings_dir / detached_id).glob("*.wav"))
        assert len(wavs) == 1, f"expected one resumed WAV, got {[w.name for w in wavs]}"
        with wave.open(str(wavs[0]), "rb") as w:
            assert w.getnframes() == 960  # 3 frames appended across the blip
        # Nothing leaked into the global current session.
        assert list(recorder_with_fake_wlk.session_dir.glob("*.wav")) == []

    def test_detached_tap_live_captions_attributed_to_its_session(
        self, client: TestClient, recorder_with_fake_wlk: Recorder, fake_wlk: FakeWlkThread
    ):
        """Session affiliation covers the live feed too: settled lines from
        a detached tap carry the detached session id, not the global
        current one."""
        detached_id = client.post("/api/tap/new-session", json={"detached": True}).json()["session"]
        pcm = b"\x10\x00" * 320
        with client.websocket_connect(f"/tap?identity=alice&name=Alice&session={detached_id}") as ws:
            ws.send_bytes(pcm)
            assert _wait_for_relay_bytes(fake_wlk, len(pcm))
            fake_wlk.push_committed("from the detached meeting")
            fake_wlk.push_committed("tail line")
            assert _wait_for_transcript_text(recorder_with_fake_wlk, "from the detached meeting")

        entries = [
            e
            for e in recorder_with_fake_wlk.transcripts.snapshot()
            if e["text"] == "from the detached meeting"
        ]
        assert entries and entries[0]["session"] == detached_id


def test_pick_tap_subprotocol_returns_match():
    """Pure helper test — the heart of the auth gate, exercised in isolation."""
    from tapscribe.auth import TAP_SUBPROTOCOL_PREFIX, pick_tap_subprotocol

    token = "ABCxyz_123-"
    proto = TAP_SUBPROTOCOL_PREFIX + token
    assert pick_tap_subprotocol([proto], token) == proto
    assert pick_tap_subprotocol(["chat.v3", proto], token) == proto
    assert pick_tap_subprotocol([proto + "wrong"], token) is None
    assert pick_tap_subprotocol([], token) is None
    # An empty expected token is never satisfied (would otherwise be a bypass).
    assert pick_tap_subprotocol([TAP_SUBPROTOCOL_PREFIX], "") is None


# ---------------------------------------------------------------------------
# Property-based fuzz of the /tap auth gate. The helper must return the
# exact PREFIX+token if (and only if) one of the offered protocols carries
# that exact string. Substring matches, prefix-of-prefix collisions, and
# unicode-normalisation tricks should all return None.
# ---------------------------------------------------------------------------

from hypothesis import given  # noqa: E402, I001
from hypothesis import strategies as st  # noqa: E402, I001

_TOKEN_ALPHABET = st.characters(
    whitelist_categories=("L", "N"),
    whitelist_characters="-_.",
)
_TOKEN_ST = st.text(alphabet=_TOKEN_ALPHABET, min_size=8, max_size=64)
_OFFER_ST = st.lists(st.text(max_size=80), max_size=5)


@given(_TOKEN_ST, _OFFER_ST)
def test_pick_tap_subprotocol_only_returns_exact_prefix_token(token: str, offered: list[str]):
    from tapscribe.auth import TAP_SUBPROTOCOL_PREFIX, pick_tap_subprotocol

    expected = TAP_SUBPROTOCOL_PREFIX + token
    result = pick_tap_subprotocol(offered, token)
    if expected in [p.strip() for p in offered]:
        assert result == expected
    else:
        assert result is None


@given(st.text(max_size=80))
def test_pick_tap_subprotocol_empty_token_always_rejects(arbitrary_offer: str):
    """An empty `expected_token` must never authenticate — accepting bare
    `tapscribe.v1.tap.` as a valid offer would be a complete auth bypass."""
    from tapscribe.auth import pick_tap_subprotocol

    assert pick_tap_subprotocol([arbitrary_offer], "") is None
