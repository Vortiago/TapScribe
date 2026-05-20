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

2. **Backend-agnostic**: future `ParakeetLiveChannel` / `CanaryLive…`
   etc. don't have built-in VADs. Gating at TapScribe's layer means
   one code path serves every live backend.

3. **Operator-tunable from the dashboard**: threshold and timings are
   `LiveConfig` fields that surface in the UI, not buried in a
   subprocess child's CLI.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

# PCM frame contract — matches the Bridge → /tap wire format. 20 ms of
# 16 kHz mono int16 = 320 samples = 640 bytes per frame. Don't reuse
# constants from bridges/ — those are JS — but they MUST agree.
SAMPLE_RATE = 16_000
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
    """

    def __init__(
        self,
        *,
        vad: VadAnalyzer,
        pre_roll_ms: int = 300,
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

    @property
    def is_open(self) -> bool:
        return self._is_open

    def feed(self, frame: bytes) -> list[bytes]:
        """Push one PCM frame through the gate. Returns the frames to
        forward to the live relay — empty list during silence, the
        frame itself during speech, and the pre-roll burst + the
        triggering frame on the silence→speech transition.

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

        emitted: list[bytes] = []
        opened_during_this_frame = False
        # Today a single feed() runs VAD at most once (640 B/frame ≤
        # 1024 B/chunk); if frame size ever grows, two transitions
        # in one feed are possible and the open/close handling below
        # would need to consider the order of events, not just the
        # final state.
        while len(self._sample_buf) >= VAD_CHUNK_BYTES:
            chunk = bytes(self._sample_buf[:VAD_CHUNK_BYTES])
            del self._sample_buf[:VAD_CHUNK_BYTES]
            event = self._vad(chunk)
            if event is None:
                continue
            if "start" in event and not self._is_open:
                self._is_open = True
                opened_during_this_frame = True
            elif "end" in event and self._is_open:
                self._is_open = False

        # After VAD has processed every full chunk available, decide
        # what to emit for THIS frame. Three cases:
        #   1. Just transitioned to open: dump pre-roll, then emit this frame.
        #   2. Currently open (no transition or was already open): emit this frame.
        #   3. Currently closed: park the frame in pre-roll for future open.
        if opened_during_this_frame:
            emitted.extend(self._pre_roll)
            self._pre_roll.clear()
            emitted.append(frame)
        elif self._is_open:
            emitted.append(frame)
        else:
            self._pre_roll.append(frame)
        return emitted


# ---------------------------------------------------------------------------
# Silero VAD wiring (production path)
# ---------------------------------------------------------------------------
#
# Lazy module-level model load: the silero-vad model is ~1 MB and ~100 ms
# to load — fine at recorder startup, expensive per /tap. Each gate gets
# its own VADIterator (which holds streaming state), but they all share
# the same loaded model.

_silero_model = None


def _get_silero_model() -> object:
    """Load (and cache) the Silero VAD model. Imports are inside the
    function so a TapScribe install without the `vad` extra doesn't
    fail at import time — the gate is constructible without Silero,
    used by tests, and only `make_silero_vad` reaches for the model."""
    global _silero_model
    if _silero_model is None:
        from silero_vad import load_silero_vad  # noqa: PLC0415

        _silero_model = load_silero_vad()
    return _silero_model


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
    """
    import numpy as np  # noqa: PLC0415
    import torch  # noqa: PLC0415
    from silero_vad import VADIterator  # noqa: PLC0415

    model = _get_silero_model()
    it = VADIterator(
        model,
        threshold=threshold,
        sampling_rate=SAMPLE_RATE,
        min_silence_duration_ms=hangover_ms,
    )

    def analyze(chunk: bytes) -> dict | None:
        # int16 → float32 in [-1, 1]. Silero accepts numpy or torch;
        # torch avoids one copy. View the byte buffer as int16 first.
        arr = np.frombuffer(chunk, dtype=np.int16)
        t = torch.from_numpy(arr.astype(np.float32) / 32768.0)
        return it(t)

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
    return SpeechGate(vad=vad, pre_roll_ms=int(config.gate_pre_roll_ms))
