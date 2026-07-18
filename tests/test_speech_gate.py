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
# min_speech_ms — pending-state warm-up
# ---------------------------------------------------------------------------


def test_min_speech_ms_holds_emission_until_threshold_reached() -> None:
    """With min_speech_ms=100, a "start" event opens the gate into a
    pending state — frames buffer privately and `is_open` stays False
    until 100 ms (5 × 20 ms frames) of post-start audio has accumulated.
    Once the threshold trips, the pre-roll burst + pending frames + the
    triggering frame are emitted in one shot."""
    # VAD says start on its first call (which fires on frame 2 since
    # 1280 B fills one 1024 B chunk). Then None forever — we never see
    # an "end", so the only thing that can confirm-open is accumulating
    # enough pending frames.
    vad = _FakeVad([{"start": 0}])
    gate = SpeechGate(vad=vad, pre_roll_ms=0, min_speech_ms=100)

    f1 = _frame(1)
    assert gate.feed(f1) == []  # buffering — VAD not run yet
    assert gate.is_open is False

    # Frame 2: VAD fires, says start → pending. Nothing emitted yet.
    f2 = _frame(2)
    assert gate.feed(f2) == []
    assert gate.is_open is False

    # Frames 3, 4, 5: still in pending (counting frames since start).
    # 100 ms / 20 ms = 5 frames threshold. After feeding 5 post-start
    # frames the confirm fires and we get them all at once.
    f3, f4, f5, f6 = _frame(3), _frame(4), _frame(5), _frame(6)
    assert gate.feed(f3) == []
    assert gate.is_open is False
    assert gate.feed(f4) == []
    assert gate.is_open is False
    assert gate.feed(f5) == []
    assert gate.is_open is False
    # Frame 6 is the 5th post-start frame → 5 × 20 ms = 100 ms → confirm.
    out = gate.feed(f6)
    assert gate.is_open is True
    # Order: pre-roll (empty) + buffered pending frames (f2..f5) + f6.
    assert out == [f2, f3, f4, f5, f6]


def test_min_speech_ms_zero_keeps_immediate_open_behaviour() -> None:
    """min_speech_ms=0 is the default: a "start" event opens the gate
    on the same frame as today, no warm-up. The existing tests above
    pin this — this test makes the intent explicit at the boundary."""
    vad = _FakeVad([{"start": 0}])
    gate = SpeechGate(vad=vad, pre_roll_ms=0, min_speech_ms=0)
    f1, f2 = _frame(1), _frame(2)
    assert gate.feed(f1) == []
    assert gate.feed(f2) == [f2]
    assert gate.is_open is True


def test_min_speech_ms_discards_brief_blip() -> None:
    """A "start" followed by an "end" before min_speech_ms elapses is
    treated as a false alarm — the candidate frames are recycled into
    pre-roll (so the next true open still recovers leading audio) and
    the gate never opens. The blip never reaches the relay."""
    # VAD cadence: chunks fire on frames 2, 4, 5, 7, 8.
    # Schedule start on call #1 (frame 2), end on call #2 (frame 4).
    vad = _FakeVad([{"start": 0}, {"end": 0}])
    gate = SpeechGate(vad=vad, pre_roll_ms=200, min_speech_ms=200)

    f1, f2, f3, f4 = _frame(1), _frame(2), _frame(3), _frame(4)
    assert gate.feed(f1) == []
    assert gate.feed(f2) == []  # VAD start → pending
    assert gate.is_open is False
    assert gate.feed(f3) == []  # still pending
    # Frame 4: VAD end → discard candidate. Gate stays closed; nothing
    # emitted; the candidate frames go back into pre-roll for the next
    # real open.
    assert gate.feed(f4) == []
    assert gate.is_open is False


def test_min_speech_ms_after_blip_subsequent_real_open_recovers_pre_roll() -> None:
    """After a discarded blip, the next real "start" should still
    flush pre-roll (the candidate's frames live in there) so we
    haven't traded false-positive suppression for missing leading
    audio on the next real utterance."""
    # Cadence note: chunks fire on frames 2, 4, 5, 7, 8.
    # start#1 (frame 2) → pending
    # end#1   (frame 4) → discard (only ~40 ms pending, well below 200 ms)
    # start#2 (frame 5) → pending again
    # then 10 quiet calls (None) so the pending accumulator can roll past
    # 200 ms (10 × 20 ms post-start frames) without an end interrupting.
    vad = _FakeVad([{"start": 0}, {"end": 0}, {"start": 0}])
    gate = SpeechGate(vad=vad, pre_roll_ms=200, min_speech_ms=200)
    frames = [_frame(i) for i in range(1, 22)]

    outputs = [gate.feed(f) for f in frames]

    # The gate must eventually confirm open within the 21-frame window
    # we fed (200 ms / 20 ms = 10 post-start frames needed; second
    # start fires on frame 5, so confirm by frame 15).
    assert any(out for out in outputs), "gate never confirmed open after the second start"
    assert gate.is_open is True


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
    pytest.importorskip("silero_vad")
    from tapscribe.speech_gate import make_silero_vad

    analyze = make_silero_vad(threshold=0.5, hangover_ms=400)
    silence_chunk = b"\x00" * VAD_CHUNK_BYTES
    out = analyze(silence_chunk)
    assert out is None or isinstance(out, dict)


class _FakeSileroModel:
    """Stands in for silero's OnnxWrapper: `VADIterator.__init__` calls
    `self.reset_states()` which delegates to `model.reset_states()` — the
    exact delegation that proves the streaming state lives on the MODEL."""

    def reset_states(self) -> None:
        pass


def test_every_gate_gets_its_own_silero_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression pin for the shared-stateful-model bug: silero's
    OnnxWrapper carries the streaming RNN state ON the model object
    (`_state`/`_context`; `VADIterator.reset_states` delegates to
    `model.reset_states`), so two gates sharing one model would interleave
    two taps' audio through a single LSTM state, and every new gate
    construction would zero the state under every other open tap. Each
    `make_silero_vad` call (= each SpeechGate) must load its OWN model."""
    pytest.importorskip("silero_vad")
    from tapscribe import speech_gate as sg

    loaded: list[object] = []

    def _fresh() -> object:
        m = _FakeSileroModel()
        loaded.append(m)
        return m

    monkeypatch.setattr(sg, "load_silero_model", _fresh)
    sg.make_silero_vad(threshold=0.5, hangover_ms=400)
    sg.make_silero_vad(threshold=0.5, hangover_ms=400)
    assert len(loaded) == 2, "each gate must trigger its own model load"
    assert loaded[0] is not loaded[1]


def test_load_silero_model_is_uncached() -> None:
    """`load_silero_model` must hand out a FRESH instance every call —
    a cached instance is exactly the shared-state bug the per-gate /
    per-thread ownership exists to prevent."""
    pytest.importorskip("silero_vad")
    from tapscribe.speech_gate import load_silero_model

    assert load_silero_model() is not load_silero_model()
