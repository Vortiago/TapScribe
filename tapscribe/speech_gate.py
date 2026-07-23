"""SpeechGate — per-tap streaming speech gate.

Sits between `TapFanOut.write_frame` and the live relay's `send`. Holds
a pre-roll ring buffer and a streaming VAD; emits PCM frames only
during detected speech (plus pre-roll on open, plus VAD-internal
hangover before close).

Operator-facing knobs live on `LiveConfig` (`gate_speech_threshold`,
`gate_hangover_ms`, `gate_pre_roll_ms`). The gate itself takes a
pre-constructed VAD analyzer so it stays tractably unit-testable —
production wires `make_silero_vad(...)`; tests pass a deterministic
fake.

Why this exists (vs. relying on WhisperLiveKit's `--vac`):

1. **Pre-roll**: WlK's VAC can only trigger reactively on the first
   loud frame — it can't go back in time. Our ring buffer flushes the
   last ~300 ms when the gate opens, recovering the leading consonants
   ("c" in "could you") that WlK's VAC was eating.

2. **Backend-agnostic**: a future `ParakeetLiveChannel` etc. won't have
   a built-in VAD. Gating at TapScribe's layer means one code path
   serves every live backend.

3. **Operator-tunable from the dashboard**: threshold and timings are
   `LiveConfig` fields that surface in the UI, not buried in a
   subprocess child's CLI.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import replace

# PCM frame contract — matches the Bridge → /tap wire format. 20 ms of
# 16 kHz mono int16 = 320 samples = 640 bytes per frame. Don't reuse
# constants from bridges/ — those are JS — but they MUST agree. The
# sample rate is the recorder's canonical one (tapscribe.audio), same
# aliasing as strip_silence / wav_predecode.
from .audio import RECORDER_SAMPLE_RATE as SAMPLE_RATE

FRAME_SAMPLES = 320
FRAME_BYTES = FRAME_SAMPLES * 2  # int16 little-endian
# Silero VAD v5 takes exactly 512 samples per inference at 16 kHz. That's
# 1024 bytes — a non-multiple of FRAME_BYTES (640), so the gate buffers
# bytes and runs VAD whenever the buffer has enough.
VAD_CHUNK_SAMPLES = 512
VAD_CHUNK_BYTES = VAD_CHUNK_SAMPLES * 2

# Streaming VAD callable signature. Production uses `silero_vad.VADIterator`
# wrapped to take raw int16 bytes. Tests pass a deterministic fake.
#
# A return value of None means "no transition this chunk"; a dict with
# key "start" or "end" signals the corresponding edge (matching
# VADIterator's own return shape).
VadAnalyzer = Callable[[bytes], dict | None]


class SpeechGate:
    """One-per-/tap streaming speech gate.

    Public surface is intentionally tiny: `feed(frame) -> list[bytes]`
    and the `is_open` flag. Per-frame state (VAD iterator, pre-roll
    ring, sample buffer) is hidden.

    Lifecycle: instantiate once per /tap WS, call `feed()` for every
    20 ms PCM frame, forward the returned frames to the live relay. No
    explicit close — the gate is GCed when the TapFanOut goes away.

    Two phases on the silence→speech transition:
      1. VAD says "start". If `min_speech_ms == 0`, that's enough to
         confirm-open immediately (legacy behaviour). Otherwise the
         gate enters a *pending* state: frames are buffered separately
         and not yet emitted.
      2. Pending → confirmed only once `min_speech_ms` of audio has
         accumulated. If VAD says "end" first, the candidate is
         discarded (brief noise blip — clicks, key taps, single
         coughs). On confirmation the pre-roll + pending buffer + the
         current frame are flushed in one burst.

    `is_open` is True only in the *confirmed* state, so dashboards see
    pending warm-up as "still quiet" rather than flicker.
    """

    def __init__(
        self,
        *,
        vad: VadAnalyzer,
        pre_roll_ms: int = 300,
        min_speech_ms: int = 0,
    ) -> None:
        self._vad = vad
        self._is_open = False
        # Ring buffer of recent frames. maxlen=0 is legal — `deque`
        # silently drops appends, so pre_roll_ms=0 just means "no
        # leading recovery" without needing a separate code path.
        self._pre_roll: deque[bytes] = deque(maxlen=max(0, pre_roll_ms // 20))
        # Sample buffer holding raw PCM bytes awaiting VAD inference.
        # Drained in VAD_CHUNK_BYTES-sized slices.
        self._sample_buf = bytearray()
        # Pending-open guard. VAD "start" with min_speech_ms>0 enters
        # pending; frames buffered here are either flushed (on
        # confirmation) or discarded (on early "end") without ever
        # reaching the relay.
        self._min_speech_ms = max(0, min_speech_ms)
        self._pending_open = False
        self._pending_frames: list[bytes] = []

    @property
    def is_open(self) -> bool:
        return self._is_open

    def feed(self, frame: bytes) -> list[bytes]:
        """Push one PCM frame through the gate. Returns the frames to
        forward to the live relay — empty list during silence or
        pending warm-up, the frame itself during confirmed speech,
        and a pre-roll + pending-buffer burst on the
        silence→confirmed-speech transition.

        Malformed frames (wrong size) are dropped silently — the
        contract is fixed-size 20 ms frames, and anything else is an
        upstream bug we don't try to interpret.
        """
        if len(frame) != FRAME_BYTES:
            return []

        # Buffer for VAD. We always run VAD on whatever PCM has accumulated,
        # regardless of gate state — the VAD iterator's internal state
        # tracks open/close transitions across calls.
        self._sample_buf.extend(frame)

        # Today a single feed() runs VAD at most once (640 B/frame ≤
        # 1024 B/chunk); if frame size ever grows, two transitions in
        # one feed are possible and we'd need to consider event order.
        # Track the latest event so the post-loop decision uses it.
        event_kind: str | None = None  # None | "start" | "end"
        while len(self._sample_buf) >= VAD_CHUNK_BYTES:
            chunk = bytes(self._sample_buf[:VAD_CHUNK_BYTES])
            del self._sample_buf[:VAD_CHUNK_BYTES]
            event = self._vad(chunk)
            if event is None:
                continue
            if "start" in event:
                event_kind = "start"
            elif "end" in event:
                event_kind = "end"

        # State machine:
        #   closed + start → confirmed-open (min_speech_ms==0) or pending
        #   pending + frame → accumulate; once >= min_speech_ms, confirm
        #   pending + end → discard candidate (false alarm)
        #   confirmed + frame → emit
        #   confirmed + end → close, drop this trailing frame
        #   closed + frame → park in pre-roll
        emitted: list[bytes] = []
        if event_kind == "start" and not self._is_open and not self._pending_open:
            if self._min_speech_ms == 0:
                self._is_open = True
                emitted.extend(self._pre_roll)
                self._pre_roll.clear()
                emitted.append(frame)
                return emitted
            self._pending_open = True
            self._pending_frames = [frame]
            return emitted

        if event_kind == "end":
            if self._pending_open:
                # False alarm — let the candidate frames seed pre-roll
                # for the next attempt so we still capture leading audio.
                for f in self._pending_frames:
                    self._pre_roll.append(f)
                self._pre_roll.append(frame)
                self._pending_open = False
                self._pending_frames = []
                return emitted
            if self._is_open:
                self._is_open = False
                return emitted
            self._pre_roll.append(frame)
            return emitted

        # No VAD transition this frame.
        if self._is_open:
            emitted.append(frame)
            return emitted
        if self._pending_open:
            self._pending_frames.append(frame)
            if len(self._pending_frames) * 20 >= self._min_speech_ms:
                self._is_open = True
                self._pending_open = False
                emitted.extend(self._pre_roll)
                self._pre_roll.clear()
                emitted.extend(self._pending_frames)
                self._pending_frames = []
            return emitted
        self._pre_roll.append(frame)
        return emitted


# ---------------------------------------------------------------------------
# Silero VAD wiring (production path)
# ---------------------------------------------------------------------------
#
# One model PER GATE, never a shared instance: silero's streaming RNN
# state lives ON the model object, so sharing one across gates would
# interleave concurrent taps' audio through a single LSTM state. See
# `load_silero_model`.


def load_silero_model() -> object:
    """Load a FRESH Silero VAD model instance — deliberately UNCACHED.

    The streaming recurrent state lives ON the model object (silero's
    OnnxWrapper keeps `_state`/`_context` there; `VADIterator` holds only
    trigger bookkeeping and its `reset_states` delegates to
    `model.reset_states`), so a process-wide cached instance is a
    correctness bug, not an optimisation: concurrent /tap gates would
    interleave their audio through one LSTM state, every new gate
    construction would zero the state under every other open tap, and a
    `get_speech_timestamps` run (strip-preview / batch strip, on worker
    threads) would corrupt a live gate's state mid-utterance. Every
    consumer owns its instance: per-gate via `make_silero_vad` below,
    per-worker-thread via `strip_silence._local_silero_model`.

    Imports are inside the function so a TapScribe install without
    onnxruntime doesn't fail at import time — the gate is constructible
    without a model (tests pass a fake analyzer), and only the production
    wiring reaches for one.

    Backed by `tapscribe.vad`, a numpy+onnxruntime port of silero's own
    ONNX path with the vendored model. That is what let `torch` and the
    `silero-vad` package stop being core dependencies (#374): upstream
    declares torch as a hard requirement and imports it at module level
    even when loading `onnx=True`, though it only ever uses it as an
    array container. `tests/test_vad_silero_port.py` pins the port
    against the real package."""
    from .vad import load_model  # noqa: PLC0415

    return load_model()


def make_silero_vad(*, threshold: float, hangover_ms: int) -> VadAnalyzer:
    """Produce a streaming VAD analyzer wrapping Silero's VADIterator.

    The returned callable takes raw int16 PCM bytes (exactly
    VAD_CHUNK_BYTES of them per call) and returns Silero's
    `{"start": ts}` / `{"end": ts}` / None directly.

    `threshold` is the speech-probability gate (0..1). `hangover_ms`
    is the post-speech silence that must elapse before Silero declares
    the burst ended — operators dial this up if their conversations
    have lots of mid-sentence breath pauses they don't want chopped
    apart, down if they want tighter sentence boundaries.

    Loads a FRESH model per call (= per gate): the streaming state lives
    on the model, not the VADIterator (see `load_silero_model`). The
    ~1 MB / ~100 ms load cost is paid off the event loop — production
    gate construction runs via `asyncio.to_thread` in `TapRelay._attach`
    (#249), so a per-gate load can't stall other taps' frames or the
    /api/state poll.
    """
    import numpy as np  # noqa: PLC0415

    from .vad import VadIterator  # noqa: PLC0415

    model = load_silero_model()
    it = VadIterator(
        model,
        threshold=threshold,
        sampling_rate=SAMPLE_RATE,
        min_silence_duration_ms=hangover_ms,
    )

    def analyze(chunk: bytes) -> dict | None:
        # int16 → float32 in [-1, 1]. numpy the whole way now that the VAD
        # takes arrays directly — one fewer copy than the old torch hop.
        arr = np.frombuffer(chunk, dtype=np.int16)
        return it(arr.astype(np.float32) / 32768.0)

    return analyze


def build_gate_for_config(config) -> SpeechGate | None:  # config: LiveConfig
    """Construct a `SpeechGate` from a `LiveConfig`, or return `None`
    when gating is handled backend-side (`gate_kind="backend"`).

    Decoupled from the LiveConfig import so this module stays
    importable in test contexts that don't carry a full live config.
    Production wiring lives in `tapscribe.tap_fan_out`, which calls
    this once per `/tap` WS open with the recorder's current
    LiveConfig.

    Tests monkey-patch this function to inject a deterministic gate
    (or a None override) without loading Silero or constructing a
    real LiveConfig.
    """
    if getattr(config, "gate_kind", "tapscribe") != "tapscribe":
        return None
    vad = make_silero_vad(
        threshold=float(config.gate_speech_threshold),
        hangover_ms=int(config.gate_hangover_ms),
    )
    return SpeechGate(
        vad=vad,
        pre_roll_ms=int(config.gate_pre_roll_ms),
        min_speech_ms=int(getattr(config, "gate_min_speech_ms", 0) or 0),
    )


def effective_gate_config(channel, config):  # channel: LiveChannel, config: LiveConfig
    """The config the per-tap gate must actually be BUILT from (and the
    gate_kind a channel's `info` must REPORT) for `channel`.

    The one seam for the capability rule "a channel with no backend-side
    VAD cannot honor gate_kind='backend'": for such a channel a carried-
    forward "backend" is coerced to "tapscribe" — `build_gate_for_config`
    would otherwise return None and the tap would feed raw UNGATED PCM
    (silence included) to the engine. The operator's persisted config
    stays untouched (it must survive the swap back to a native-VAD
    channel); every consumer derives the effective value from here:
    `TapRelay` when building a tap's gate, and channels' info-seeding
    when reporting `gate_kind`. (`/api/live/start` shares the predicate
    but REJECTS an explicit gate_kind="backend" request with a 400
    instead of coercing — see the route.)

    `supports_native_vad` deliberately defaults to False when absent —
    a channel that forgets to declare it gets a gate built (the safe
    direction), matching the route's conservative default. Same duck
    typing as `build_gate_for_config` above.
    """
    if getattr(config, "gate_kind", "tapscribe") == "backend" and not getattr(
        channel, "supports_native_vad", False
    ):
        return replace(config, gate_kind="tapscribe")
    return config
