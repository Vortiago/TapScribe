"""End-to-end tests for the /tap WebSocket — the merged Bridge contract.

We spin up a fake whisperlivekit-server in-process and point the Recorder
at it (via LiveConfig host/port). Then we open a /tap WS via TestClient,
send PCM bytes, and verify both the WAV is written AND the relay
forwarded bytes to the fake WlK AND settled-lines pushed by the fake
landed in recorder.transcripts.
"""

from __future__ import annotations

import time
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import (
    FakeWlkThread,  # type: ignore[import-not-found]  # noqa: E402  # pytest puts tests/ on sys.path; a `from tests.X import` form collides with NeMo's installed top-level `tests` package
)
from fastapi.testclient import TestClient

from tapscribe import config as _config
from tapscribe.app import app, get_recorder
from tapscribe.live import LiveConfig
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
    (tmp_path / "config").mkdir()
    (tmp_path / "recordings").mkdir()

    r = Recorder(
        recordings_dir=tmp_path / "recordings",
        config_dir=tmp_path / "config",
        # Relay-focused tests use near-silent synthetic PCM that real
        # Silero would block — pin gate_kind="backend" so they exercise
        # the relay end-to-end without the gate eating their bytes.
        live_config=LiveConfig(
            model="tiny.en",
            language="en",
            host="localhost",
            port=fake_wlk.port,
            gate_kind="backend",
        ),
        use_mlx=False,
        auth_password_file=tmp_path / ".auth-password",
    )

    # The relay opens only when LiveChannel.running() is True. Force-mark
    # the channel as running by injecting a fake proc that .poll() returns
    # None (i.e., still alive). The real subprocess.Popen would do the
    # same; we just don't have one in tests.
    class _FakeProc:
        def poll(self):
            return None  # "alive"

    r.live._proc = _FakeProc()
    return r


@pytest.fixture
def client(recorder_with_fake_wlk: Recorder) -> Iterator[TestClient]:
    app.dependency_overrides[get_recorder] = lambda: recorder_with_fake_wlk
    app.state.recorder = recorder_with_fake_wlk
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


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

    @pytest.fixture
    def auth_client(self, recorder_with_fake_wlk: Recorder, monkeypatch: pytest.MonkeyPatch):
        """Same client as the parent test module but with AUTH_ENABLED so
        the WS handler runs the subprotocol gate."""
        monkeypatch.setattr(_config, "AUTH_ENABLED", True)
        app.dependency_overrides[get_recorder] = lambda: recorder_with_fake_wlk
        app.state.recorder = recorder_with_fake_wlk
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

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
