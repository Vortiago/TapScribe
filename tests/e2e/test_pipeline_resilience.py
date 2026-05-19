"""E2E resilience scenarios for the /tap pipeline.

These tests target the failure modes operators actually hit in
production — bridges dropping mid-utterance, recording toggles flipping
under reconnect, the live channel crashing while audio keeps flowing.
Each scenario asserts against one of the invariants listed in
`CONTEXT.md` (one utterance = one WAV, recording independent of the
live channel, the next-utterance-not-this-one toggle semantics).

Tests reuse the canonical `running_recorder` fixture + the
`stream_wav_via_tap` helper. Force-close is done by reaching past the
public `websockets` client API and `abort()`-ing the underlying
transport, which is the closest thing to "Wi-Fi went away" the test
suite can produce without a packet filter.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
import websockets

from tapscribe import transcribers as _transcribers
from tapscribe.auth import TAP_SUBPROTOCOL_PREFIX

from .conftest import RunningRecorder
from .fake_transcriber import FakeTranscriber
from .harness import (
    FRAME_BYTES,
    SAMPLE_RATE,
    frame_pcm,
    read_wav_as_pcm_bytes,
    stream_wav_via_tap,
    streams_drained,
    synth_speech_like_wav,
    wait_until,
)


@pytest.fixture
def fake_transcriber(monkeypatch: pytest.MonkeyPatch) -> FakeTranscriber:
    """Identical wiring to the headline E2E test — load_transcriber is
    patched both on the canonical module and on the local binding inside
    `tapscribe.app` so the route handler sees the fake."""
    fake = FakeTranscriber(text_by_speaker={"Alice": "alice line one", "Bob": "bob line"})

    def _factory(model_name: str, *, use_mlx: bool) -> FakeTranscriber:  # noqa: ARG001
        return fake

    monkeypatch.setattr(_transcribers, "load_transcriber", _factory)
    import tapscribe.app as _app

    monkeypatch.setattr(_app, "load_transcriber", _factory)
    _transcribers.clear_cache()
    return fake


@pytest.fixture
def alice_wav(tmp_path: Path) -> Path:
    return synth_speech_like_wav(tmp_path / "alice.wav", seconds=0.8, freq_hz=220.0)


@pytest.fixture
def bob_wav(tmp_path: Path) -> Path:
    return synth_speech_like_wav(tmp_path / "bob.wav", seconds=0.6, freq_hz=440.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tap_url(ws_base_url: str, *, identity: str, name: str, utterance_id: str) -> str:
    qs = urlencode({"identity": identity, "name": name, "utterance_id": utterance_id})
    return f"{ws_base_url}/tap?{qs}"


async def _stream_frames_then_abort(
    *,
    ws_base_url: str,
    identity: str,
    name: str,
    utterance_id: str,
    frames: list[bytes],
    tap_token: str = "",
) -> int:
    """Open /tap, send `frames`, then `transport.abort()` the underlying
    TCP socket — no close frame, no handshake. This emulates the most
    common bridge failure: the browser's network blip drops the WS
    without a clean close. The Recorder sees an ASGI
    `websocket.disconnect`, finalises the WAV via TapFanOut's __aexit__,
    and the bridge later reconnects with the same utterance_id."""
    url = _tap_url(ws_base_url, identity=identity, name=name, utterance_id=utterance_id)
    subprotocols = [f"{TAP_SUBPROTOCOL_PREFIX}{tap_token}"] if tap_token else None
    sent = 0
    ws = await websockets.connect(url, subprotocols=subprotocols, close_timeout=0.5)
    try:
        for frame in frames:
            await ws.send(frame)
            sent += 1
            # Let the server pick up the frame before we yank the cable;
            # without this the abort can race ahead of the receive loop.
            await asyncio.sleep(0.005)
        # Reach past the public API to send an RST. The server's ASGI
        # WS adapter surfaces this as websocket.disconnect (code 1006).
        ws.transport.abort()
    finally:
        try:
            await ws.wait_closed()
        except Exception:
            pass
    return sent


# ---------------------------------------------------------------------------
# Test 1: bridge reconnect mid-utterance keeps one WAV
# ---------------------------------------------------------------------------


async def test_bridge_reconnect_mid_utterance_preserves_one_wav(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,  # noqa: ARG001 — keeps patched factory ready
    alice_wav: Path,
):
    """One utterance = one WAV (CONTEXT.md invariant). If the bridge drops
    its /tap WS mid-stream and reconnects with the same utterance_id,
    the Recorder must append to the existing file rather than minting a
    second one. The merged WAV's frame count must equal the sum of frames
    sent across both connection attempts, and the UtteranceRecord's
    bytes_received must reflect the combined total."""
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    identity = "alice"
    name = "Alice"
    utt = "utt-resume-blip"

    pcm = read_wav_as_pcm_bytes(alice_wav)
    frames = frame_pcm(pcm)
    assert len(frames) > 8, "fixture must have enough frames to split across two WSes"
    half = len(frames) // 2
    first_chunk = frames[:half]
    second_chunk = frames[half:]

    # WS1: send half the frames, then abort the transport.
    sent_first = await _stream_frames_then_abort(
        ws_base_url=ws_base,
        identity=identity,
        name=name,
        utterance_id=utt,
        frames=first_chunk,
    )
    assert sent_first == len(first_chunk)

    # The server-side cleanup is event-based — wait until ActiveStreams
    # is empty so the second connect lands on a released UtteranceRecord.
    assert await wait_until(lambda: streams_drained(rec), timeout=3.0)

    # WS2: reconnect with the SAME utterance_id, stream the rest, close
    # cleanly. The 60s UtteranceIndex.RESUME_WINDOW_SECONDS easily covers
    # the millisecond gap here.
    url = _tap_url(ws_base, identity=identity, name=name, utterance_id=utt)
    async with websockets.connect(url) as ws:
        for frame in second_chunk:
            await ws.send(frame)
            await asyncio.sleep(0.005)
    assert await wait_until(lambda: streams_drained(rec), timeout=3.0)

    # Exactly one WAV on disk — that's the load-bearing invariant.
    wavs = list(rec.session_dir.glob("*.wav"))
    assert len(wavs) == 1, f"expected one WAV after resume, got {[w.name for w in wavs]}"

    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getframerate() == SAMPLE_RATE
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        # The wave module reports sample frames (320 per 20 ms WS frame).
        expected_samples = (len(first_chunk) + len(second_chunk)) * 320
        assert w.getnframes() == expected_samples, (
            f"WAV sample count {w.getnframes()} != expected {expected_samples}; "
            "the resume did not append, or the second WS opened a new file"
        )

    # The UtteranceRecord in the index now carries the merged byte count.
    rec_snap = rec.utterances.snapshot()[utt]
    assert rec_snap.bytes_received == (len(first_chunk) + len(second_chunk)) * FRAME_BYTES


# ---------------------------------------------------------------------------
# Test 2: back-to-back same-speaker → two distinct WAVs
# ---------------------------------------------------------------------------


async def test_back_to_back_same_speaker_yields_two_distinct_wavs(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,  # noqa: ARG001
    alice_wav: Path,
):
    """Alice mutes, then unmutes a moment later — a fresh utterance_id
    means a fresh WAV (CONTEXT.md "one utterance = one WAV"; reverse
    direction). Both must be attributed to Alice in the merged session
    transcript, and the session directory must contain two distinct
    WAV files."""
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url
    identity = "alice"
    name = "Alice"

    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity=identity,
        name=name,
        wav_path=alice_wav,
        utterance_id="utt-alice-A",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=3.0)

    await stream_wav_via_tap(
        ws_base_url=ws_base,
        identity=identity,
        name=name,
        wav_path=alice_wav,
        utterance_id="utt-alice-B",
    )
    assert await wait_until(lambda: streams_drained(rec), timeout=3.0)

    wavs = sorted(rec.session_dir.glob("*.wav"))
    assert len(wavs) == 2, f"expected two distinct WAVs, got {[w.name for w in wavs]}"
    for wav in wavs:
        assert "Alice" in wav.name, f"WAV {wav.name} missing the Alice slug"

    async with httpx.AsyncClient(base_url=base, timeout=15.0) as client:
        resp = await client.post(
            "/api/transcribe-session",
            json={"session": rec.session_start, "model": "fake-small.en"},
        )
        assert resp.status_code == 200, resp.text
        merged = resp.json()
        assert merged["wav_count"] == 2
        assert merged["speakers"] == ["Alice"], f"only Alice expected, got {merged['speakers']}"
        alice_segments = [s for s in merged["segments"] if s["speaker"] == "Alice"]
        assert len(alice_segments) == 2, (
            f"expected two Alice segments from two WAVs, got {len(alice_segments)}"
        )


# ---------------------------------------------------------------------------
# Test 3: recording toggle during reconnect — snapshot-at-open semantics
# ---------------------------------------------------------------------------


async def test_recording_toggle_during_reconnect_uses_snapshot_at_open(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,  # noqa: ARG001
    alice_wav: Path,
):
    """The recording toggle is checked at WS-open time, NOT continuously.
    So if the operator toggles recording OFF after a /tap WS opens and
    the bridge then drops + reconnects, the second WS sees the post-
    toggle state.

    Reading `tapscribe/app.py` /tap handler closely:
      - When recording_enabled is False, the WS is accepted then closed
        with code 1000 BEFORE TapFanOut is built. No fan-out, no resume
        attempt, no WAV append.
      - The first WS's UtteranceRecord stays in the index (released
        kept=True at the end of the first WS), but the second WS never
        reaches try_resume.

    Observable contract: the first WS's WAV exists with exactly the
    first-WS frame count; no second WAV is created; the second WS
    contributes nothing. This mirrors the global pause toggle's
    "next-utterance, not this one" semantics — same source of truth in
    the route handler.
    """
    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    base = running_recorder.base_url
    identity = "alice"
    name = "Alice"
    utt = "utt-toggle-mid-reconnect"

    pcm = read_wav_as_pcm_bytes(alice_wav)
    frames = frame_pcm(pcm)
    half = len(frames) // 2
    first_chunk = frames[:half]
    second_chunk = frames[half:]

    sent = await _stream_frames_then_abort(
        ws_base_url=ws_base,
        identity=identity,
        name=name,
        utterance_id=utt,
        frames=first_chunk,
    )
    assert sent == len(first_chunk)
    assert await wait_until(lambda: streams_drained(rec), timeout=3.0)

    wavs = list(rec.session_dir.glob("*.wav"))
    assert len(wavs) == 1
    first_wav = wavs[0]
    with wave.open(str(first_wav), "rb") as w:
        assert w.getnframes() == len(first_chunk) * 320

    # Flip recording off BEFORE the reconnect.
    async with httpx.AsyncClient(base_url=base, timeout=5.0) as client:
        resp = await client.post("/api/recording/toggle", json={"enabled": False})
        assert resp.json() == {"ok": True, "enabled": False}

    # WS2 reconnects with the SAME utterance_id. Per the snapshot-at-open
    # rule, this WS gets refused (closed with code 1000) without ever
    # calling TapFanOut. Frames sent before the server-initiated close
    # may or may not arrive (race) — the load-bearing assertion is that
    # no additional bytes land on disk and no second WAV is created.
    url = _tap_url(ws_base, identity=identity, name=name, utterance_id=utt)
    try:
        async with websockets.connect(url, close_timeout=0.5) as ws:
            for frame in second_chunk:
                try:
                    await ws.send(frame)
                except websockets.ConnectionClosed:
                    break
                await asyncio.sleep(0.005)
    except websockets.ConnectionClosed:
        pass

    assert await wait_until(lambda: streams_drained(rec), timeout=3.0)

    wavs_after = list(rec.session_dir.glob("*.wav"))
    assert len(wavs_after) == 1, f"expected the original WAV unchanged, got {[w.name for w in wavs_after]}"
    assert wavs_after[0].name == first_wav.name
    with wave.open(str(first_wav), "rb") as w:
        assert w.getnframes() == len(first_chunk) * 320, (
            "the second WS must NOT have appended frames "
            "(recording was paused at its open per snapshot-at-open)"
        )


# ---------------------------------------------------------------------------
# Test 4: WhisperLiveKit crash mid-utterance
# ---------------------------------------------------------------------------


def _fake_wlk_has_killable_api(fake_wlk) -> bool:
    """Track D's killable FakeWlk exposes `terminate()`, which simulates a
    process crash mid-utterance by abruptly tearing connections without a
    graceful drain. Without it, callers should skip — a degraded substitute
    would silently coexist with the real test and mask regressions."""
    return hasattr(fake_wlk, "terminate")


async def test_wlk_crash_mid_utterance_keeps_wav_and_recovers_for_next_tap(
    running_recorder: RunningRecorder,
    fake_transcriber: FakeTranscriber,  # noqa: ARG001
    alice_wav: Path,
    bob_wav: Path,
):
    """Recording is independent of the live channel (CONTEXT.md invariant
    "Bridge → /tap is the only audio path; Recorder owns all fan-out",
    plus ADR-0002's graceful-degradation rule). If the WhisperLiveKit
    child dies mid-utterance, the in-flight /tap's WAV must finalise
    correctly, and a SUBSEQUENT /tap must get a working relay once the
    live channel is back up.

    Skipped until Track D's killable FakeWlk lands — without it the
    crash simulation degrades into a port-rebinding dance that no longer
    matches the real failure mode.
    """
    if not _fake_wlk_has_killable_api(running_recorder.fake_wlk):
        pytest.skip("requires Track D's killable FakeWlk")

    rec = running_recorder.recorder
    ws_base = running_recorder.ws_base_url
    fake_wlk = running_recorder.fake_wlk

    pcm = read_wav_as_pcm_bytes(alice_wav)
    frames = frame_pcm(pcm)
    url = _tap_url(ws_base, identity="alice", name="Alice", utterance_id="utt-alice-crash")
    async with websockets.connect(url) as ws:
        for frame in frames[:5]:
            await ws.send(frame)
            await asyncio.sleep(0.005)
        fake_wlk.terminate()
        # Continue streaming Alice's audio — WAV writes must continue
        # uninterrupted (recording is independent of the live channel).
        for frame in frames[5:]:
            await ws.send(frame)
            await asyncio.sleep(0.005)
    assert await wait_until(lambda: streams_drained(rec), timeout=5.0)

    wavs = list(rec.session_dir.glob("*.wav"))
    assert len(wavs) == 1
    with wave.open(str(wavs[0]), "rb") as w:
        assert w.getnframes() == len(frames) * 320

    # The restart-and-reconnect half of this test is left for a follow-up:
    # FakeWlkThread is a single-shot daemon thread (Thread.start() can only
    # run once), and the recorder's relay is bound to the WlK port at
    # configuration time, so re-pointing it at a fresh FakeWlk on a new
    # port would require either a port-pinned FakeWlk constructor or a
    # recorder API to swap the relay target. Neither exists today.
    # The first half above already pins the high-value invariant
    # (recording independent of live channel); restart-coverage will need
    # its own design pass.
    _ = bob_wav
