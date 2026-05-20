"""Tests for SpeechGate — the per-tap speech gate that sits between
TapFanOut.write_frame and WlKRelay.send.

The gate's job is to forward PCM only during detected speech bursts
(plus pre-roll on open, plus hangover on close), so the live backend
gets clean speech-only audio without head/tail words being eaten by
the backend's own VAC. See docs/threading-design conversation history
for why this is structured the way it is.

These tests run on a fake VAD analyzer so they don't load Silero on
every test run. A separate integration test exercises the real Silero
VADIterator path.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from tapscribe.speech_gate import FRAME_BYTES, VAD_CHUNK_BYTES, SpeechGate


class _FakeVad:
    """Test VAD: emits queued events in order — one per VAD chunk call.

    Each call to `__call__(chunk)` pops the next event from the queue.
    None means "no transition this chunk." A dict with key "start" /
    "end" signals a transition.
    """

    def __init__(self, events: Iterable[dict | None] = ()) -> None:
        self._events: list[dict | None] = list(events)
        self.calls = 0

    def __call__(self, chunk: bytes) -> dict | None:
        self.calls += 1
        if not self._events:
            return None
        return self._events.pop(0)


def _silence_frame() -> bytes:
    return b"\x00" * FRAME_BYTES


def _frame(seed: int) -> bytes:
    # Distinct byte pattern per frame so tests can tell them apart by
    # equality. The first 4 bytes encode `seed` so each frame is unique
    # even with many frames in flight (modulo-of-product collisions
    # otherwise made nearby frames identical).
    head = seed.to_bytes(4, "little", signed=False)
    return head + bytes((i + (seed & 0xFF)) % 256 for i in range(FRAME_BYTES - 4))


# ---------------------------------------------------------------------------
# Initial state + silence
# ---------------------------------------------------------------------------


def test_gate_starts_closed() -> None:
    gate = SpeechGate(vad=_FakeVad([]), pre_roll_ms=0)
    assert gate.is_open is False


def test_gate_emits_nothing_for_silence_while_closed() -> None:
    gate = SpeechGate(vad=_FakeVad([]), pre_roll_ms=0)
    for _ in range(10):
        assert gate.feed(_silence_frame()) == []
    assert gate.is_open is False


# ---------------------------------------------------------------------------
# Open transition + pre-roll
# ---------------------------------------------------------------------------


def test_gate_opens_on_vad_start_event_and_emits_current_frame() -> None:
    # 20ms frame = 320 samples = 640 bytes. VAD chunk = 512 samples = 1024
    # bytes. Two frames fill one VAD chunk (with 256 bytes left over).
    # No pre-roll → only the triggering frame is emitted.
    vad = _FakeVad([{"start": 0}])
    gate = SpeechGate(vad=vad, pre_roll_ms=0)

    # Frame 1: 640 bytes buffered; not enough for VAD chunk.
    assert gate.feed(_frame(1)) == []
    assert gate.is_open is False
    assert vad.calls == 0

    # Frame 2: 1280 bytes buffered → VAD runs on first 1024 → "start" → gate opens.
    f2 = _frame(2)
    assert gate.feed(f2) == [f2]
    assert gate.is_open is True
    assert vad.calls == 1


def test_pre_roll_is_emitted_on_open_transition() -> None:
    # pre_roll_ms=60 → 3 frames of pre-roll capacity.
    pre1, pre2 = _frame(11), _frame(12)
    speech = _frame(13)
    vad = _FakeVad([{"start": 0}])
    gate = SpeechGate(vad=vad, pre_roll_ms=60)

    # First two frames feed pre-roll (each is 640 bytes; total 1280 bytes
    # after second). But VAD hasn't said start yet — _FakeVad with
    # queued events returns None until called. Wait — the first VAD
    # chunk is consumed by frame 2's feed, returning {"start": 0}. So
    # we need to verify pre1 sits in pre-roll then gets flushed.
    assert gate.feed(pre1) == []  # 640 bytes — no VAD run yet
    # Two frames in: 1280 buffered → VAD runs and says start.
    out = gate.feed(pre2)
    # pre1 was in pre-roll; pre2 was the triggering frame. Emit
    # pre-roll then the trigger frame.
    assert out == [pre1, pre2]
    assert gate.is_open is True

    # Next frame: gate is open, emit it directly.
    assert gate.feed(speech) == [speech]


def test_pre_roll_holds_all_frames_when_ring_not_full() -> None:
    """20 ms frame, 1024 B VAD chunk → VAD calls land on frames 2, 4, 5
    in that cadence. With events = [None, None, {"start": 0}] the third
    VAD call (frame 5) opens the gate. Pre-roll capacity is generous
    (10 frames) so all four pre-trigger frames are in flight."""
    f1, f2, f3, f4, trigger = (_frame(i) for i in range(1, 6))
    vad = _FakeVad([None, None, {"start": 0}])
    gate = SpeechGate(vad=vad, pre_roll_ms=200)  # maxlen=10

    for f in (f1, f2, f3, f4):
        assert gate.feed(f) == []
    assert gate.is_open is False  # VAD calls #1 (frame 2) and #2 (frame 4) both None

    out = gate.feed(trigger)  # frame 5 = VAD call #3 → "start"
    assert gate.is_open is True
    assert out == [f1, f2, f3, f4, trigger]


def test_pre_roll_evicts_oldest_when_ring_full() -> None:
    """Same VAD cadence, ring capped at 2 frames → only the most recent
    2 closed-state frames are in pre-roll when the gate opens; the
    earlier ones got evicted as new frames rotated through."""
    f1, f2, f3, f4, trigger = (_frame(i) for i in range(1, 6))
    vad = _FakeVad([None, None, {"start": 0}])
    gate = SpeechGate(vad=vad, pre_roll_ms=40)  # maxlen=2

    for f in (f1, f2, f3, f4):
        assert gate.feed(f) == []
    out = gate.feed(trigger)
    assert gate.is_open is True
    assert out == [f3, f4, trigger]


# ---------------------------------------------------------------------------
# Closed transition + hangover
# ---------------------------------------------------------------------------


def test_gate_closes_on_vad_end_event() -> None:
    """When VAD reports end, the gate flips to closed and stops emitting.
    VAD cadence: chunks fire on frames 2, 4, 5, 7, 8. Schedule events
    to fire start on call #1 (frame 2) and end on call #3 (frame 5)."""
    vad = _FakeVad([{"start": 0}, None, {"end": 0}])
    gate = SpeechGate(vad=vad, pre_roll_ms=0)

    f1, f2, f3, f4, f5 = (_frame(i) for i in range(1, 6))
    assert gate.feed(f1) == []
    assert gate.feed(f2) == [f2]  # VAD call #1 → start
    assert gate.is_open is True

    # Frames 3, 4 (still open): emitted through.
    assert gate.feed(f3) == [f3]
    assert gate.feed(f4) == [f4]  # VAD call #2 → None, still open

    # Frame 5: VAD call #3 → end. By the time VAD says end, we've been
    # in post-speech silence (handled inside VAD's own hangover) for
    # the configured duration, so this trailing frame is silence we no
    # longer want forwarded.
    out_close = gate.feed(f5)
    assert gate.is_open is False
    assert out_close == []


def test_gate_emits_all_frames_while_open() -> None:
    """Inside the open window, every frame goes through unchanged."""
    vad = _FakeVad([{"start": 0}])  # open after second frame; never closes
    gate = SpeechGate(vad=vad, pre_roll_ms=0)
    gate.feed(_frame(1))
    gate.feed(_frame(2))  # opens
    assert gate.is_open

    for i in range(3, 20):
        f = _frame(i)
        assert gate.feed(f) == [f], f"frame {i} suppressed while gate was open"


def test_gate_drops_silence_again_after_close() -> None:
    """After end, no more VAD events queued → FakeVad returns None
    forever → silence drops on the floor."""
    vad = _FakeVad([{"start": 0}, None, {"end": 0}])
    gate = SpeechGate(vad=vad, pre_roll_ms=0)
    for i in range(1, 6):
        gate.feed(_frame(i))
    assert gate.is_open is False
    for i in range(6, 16):
        assert gate.feed(_frame(i)) == []


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


def test_gate_ignores_frame_with_wrong_size() -> None:
    """20ms frame is fixed-size (640 bytes). Anything else is a bug
    upstream — drop the frame, don't try to interpret it."""
    gate = SpeechGate(vad=_FakeVad([]), pre_roll_ms=0)
    assert gate.feed(b"\x00" * 100) == []
    assert gate.feed(b"\x00" * (FRAME_BYTES + 1)) == []
    # Internal buffer remained empty, so a subsequent valid frame doesn't
    # carry the rejected bytes.
    assert gate.feed(b"\x00" * FRAME_BYTES) == []


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


def test_constants_match_protocol() -> None:
    """20ms PCM frame @ 16kHz mono int16 = 640 bytes (320 samples).
    Silero v5 wants 512 samples per inference at 16kHz = 1024 bytes."""
    assert FRAME_BYTES == 640
    assert VAD_CHUNK_BYTES == 1024


# ---------------------------------------------------------------------------
# Silero smoke test
# ---------------------------------------------------------------------------
#
# Loading the real Silero model on every test run is slow, so this lives
# behind a marker and only runs when silero-vad is installed. It exists
# to catch the wiring-level "did we hand Silero something it accepts"
# bugs that the FakeVad tests can't catch.


def test_silero_vad_analyzer_accepts_pcm_chunks() -> None:
    """Smoke test: real Silero VAD eats VAD_CHUNK_BYTES-sized int16 PCM
    buffers without crashing. We don't assert on the return value —
    Silero's behavior on synthetic silence/noise is sensitive enough
    that committing to a specific outcome would make this test flaky.
    """
    silero_vad = pytest.importorskip("silero_vad")  # noqa: F841
    from tapscribe.speech_gate import make_silero_vad

    analyze = make_silero_vad(threshold=0.5, hangover_ms=400)
    silence_chunk = b"\x00" * VAD_CHUNK_BYTES
    out = analyze(silence_chunk)
    assert out is None or isinstance(out, dict)
