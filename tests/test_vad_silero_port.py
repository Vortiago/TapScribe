"""Differential test: `tapscribe.vad` must match the real silero-vad exactly.

`tapscribe/vad/` claims to be a faithful PORT of upstream's `OnnxWrapper`,
`VADIterator` and `get_speech_timestamps` with numpy in place of torch's array
plumbing (see the package docstring, and #374 for why torch had to go). A claim
like that is only worth anything if something checks it, and hand-written
expected values wouldn't: they'd encode whatever the port happens to do.

So these tests run BOTH implementations over the same audio and assert identical
output. That makes upstream the source of truth, which is exactly the property
the port needs.

They `importorskip` silero-vad, so they run wherever upstream is installed (this
dev box, and any CI leg that still has it) and quietly no-op on an install that
has dropped it — which is now the default. Same pattern as the repo's other
upstream-contract smoke tests (CLAUDE.md).
"""

from __future__ import annotations

import numpy as np
import pytest

from tapscribe.vad import VadIterator, load_model, speech_timestamps

silero_vad = pytest.importorskip("silero_vad", reason="upstream silero-vad not installed")
torch = pytest.importorskip("torch", reason="upstream silero-vad needs torch")

SAMPLE_RATE = 16_000


def _speechlike(seconds: float = 6.0, seed: int = 1234) -> np.ndarray:
    """Deterministic audio with clear speech/silence structure.

    Not real speech, but that's fine and arguably better: what's under test is
    that two implementations agree window-for-window, and a synthetic signal
    with hard on/off boundaries exercises the trigger, the hysteresis and the
    padding/merge arithmetic without depending on a fixture file.
    """
    rng = np.random.default_rng(seed)
    n = int(SAMPLE_RATE * seconds)
    t = np.arange(n) / SAMPLE_RATE
    # Voiced-ish: a low fundamental plus harmonics, amplitude-modulated.
    tone = (
        0.5 * np.sin(2 * np.pi * 140 * t)
        + 0.3 * np.sin(2 * np.pi * 280 * t)
        + 0.2 * np.sin(2 * np.pi * 560 * t)
    )
    tone *= 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t)
    audio = (tone + 0.01 * rng.standard_normal(n)).astype(np.float32)
    # Punch three silences in, of different lengths, so min_silence and the
    # gap-splitting padding branch both get exercised.
    for start_s, dur_s in ((1.2, 0.9), (3.0, 0.35), (4.6, 1.1)):
        a = int(start_s * SAMPLE_RATE)
        b = min(a + int(dur_s * SAMPLE_RATE), n)  # clamped: callers ask for clips shorter than 4.6 s
        if a >= n:
            break
        audio[a:b] = 0.001 * rng.standard_normal(b - a)
    return np.clip(audio, -1.0, 1.0).astype(np.float32)


def test_per_window_probabilities_match_upstream():
    """The model wrapper itself: same windows in, same probabilities out.

    This is the foundation — if the rolling `_state`/`_context` handling drifted,
    every higher-level assertion below would be comparing two different signals.
    """
    audio = _speechlike(2.0)
    ours = load_model()
    theirs = silero_vad.load_silero_vad(onnx=True)

    mine: list[float] = []
    upstream: list[float] = []
    for i in range(0, len(audio) - 512, 512):
        window = audio[i : i + 512]
        mine.append(ours(window, SAMPLE_RATE).item())
        upstream.append(theirs(torch.from_numpy(window), SAMPLE_RATE).item())

    assert len(mine) > 30, "expected a meaningful number of windows"
    np.testing.assert_allclose(mine, upstream, rtol=0, atol=0)


def test_streaming_iterator_matches_upstream():
    """The live SpeechGate's path: identical start/end events, in order."""
    audio = _speechlike()
    mine = VadIterator(load_model(), threshold=0.5, min_silence_duration_ms=100)
    upstream = silero_vad.VADIterator(
        silero_vad.load_silero_vad(onnx=True),
        threshold=0.5,
        sampling_rate=SAMPLE_RATE,
        min_silence_duration_ms=100,
    )

    my_events, their_events = [], []
    for i in range(0, len(audio) - 512, 512):
        window = audio[i : i + 512]
        if (event := mine(window)) is not None:
            my_events.append(event)
        if (event := upstream(torch.from_numpy(window))) is not None:
            their_events.append(event)

    assert my_events, "expected the synthetic signal to trigger at least one event"
    assert my_events == their_events


@pytest.mark.parametrize(
    ("min_silence_ms", "pad_ms"),
    [
        (100, 30),  # upstream defaults
        (300, 100),  # what strip-silence's dashboard knobs reach for
        (50, 0),  # tight boundaries, no padding
    ],
)
def test_speech_timestamps_match_upstream(min_silence_ms, pad_ms):
    """Strip-silence's path, across the knob range operators actually drag.

    Region boundaries are what get written to disk as separate WAVs, so an
    off-by-one here is audible — a clipped leading consonant.
    """
    audio = _speechlike()
    mine = speech_timestamps(
        audio,
        load_model(),
        sampling_rate=SAMPLE_RATE,
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=pad_ms,
    )
    upstream = silero_vad.get_speech_timestamps(
        torch.from_numpy(audio),
        silero_vad.load_silero_vad(onnx=True),
        sampling_rate=SAMPLE_RATE,
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=pad_ms,
    )
    assert mine == upstream


def test_speech_timestamps_on_silence_match_upstream():
    """Whole-file silence — the branch strip-silence special-cases downstream."""
    audio = (0.0005 * np.random.default_rng(7).standard_normal(SAMPLE_RATE * 3)).astype(np.float32)
    mine = speech_timestamps(audio, load_model(), sampling_rate=SAMPLE_RATE)
    upstream = silero_vad.get_speech_timestamps(
        torch.from_numpy(audio), silero_vad.load_silero_vad(onnx=True), sampling_rate=SAMPLE_RATE
    )
    assert mine == upstream == []


def test_model_instances_do_not_share_state():
    """The invariant the whole threading story rests on: two models fed
    different audio must not influence each other. A shared instance would
    interleave concurrent /tap gates through one LSTM state."""
    audio = _speechlike(1.5)
    solo = load_model()
    alone = [solo(audio[i : i + 512], SAMPLE_RATE).item() for i in range(0, len(audio) - 512, 512)]

    a, b = load_model(), load_model()
    interleaved = []
    noise = (0.2 * np.random.default_rng(9).standard_normal(len(audio))).astype(np.float32)
    for i in range(0, len(audio) - 512, 512):
        interleaved.append(a(audio[i : i + 512], SAMPLE_RATE).item())
        b(noise[i : i + 512], SAMPLE_RATE)  # a second gate running concurrently

    np.testing.assert_allclose(alone, interleaved, rtol=0, atol=0)
