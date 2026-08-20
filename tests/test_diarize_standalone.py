"""The standalone engine's wiring: VAD segmentation → windows → clustering →
absolute-time spans.

Synthetic tones and a stand-in embedder that keys on the dominant mel bin, so
"two speakers" is exact rather than approximate. The real model's separation is
`test_diarize_engine.py`; what is tested here is everything around it — which
audio becomes windows, and how labels become spans an interval join can use.

The VAD is the real `speech_timestamps` state machine driven by a loud-is-speech
stand-in for the model, so the segmentation logic is the shipped one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from tapscribe.diarizers.base import AudioClip
from tapscribe.text import parse_iso
from tapscribe.diarizers.standalone import FRAME_SHIFT_S, StandaloneDiarizer

RATE = 16000
T0 = datetime(2026, 8, 20, 9, 0, 0, tzinfo=UTC)


class LoudIsSpeech:
    """Stands in for `SileroVad`: anything above the noise floor is speech."""

    def reset_states(self, batch_size: int = 1) -> None:
        pass

    def __call__(self, x, sr):
        return np.array([[1.0 if float(np.abs(x).max()) > 0.05 else 0.0]], dtype=np.float32)


class BinEmbedder:
    """One unit vector per window, pointing along the window's loudest mel bin —
    so one tone is one direction and two tones are orthogonal."""

    engine = "bins"

    def embed(self, windows):
        out = np.zeros((len(windows), 512))
        for i, window in enumerate(windows):
            out[i, int(np.argmax(window.mean(axis=0)))] = 1.0
        return out


def _tone(hz: float, seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (0.3 * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE), dtype=np.float32)


@pytest.fixture
def engine() -> StandaloneDiarizer:
    return StandaloneDiarizer(BinEmbedder(), vad=LoudIsSpeech(), threshold=0.7, max_speakers=8)


def _seconds(result, label: str) -> list[tuple[float, float]]:
    """One Voice's spans as offsets from T0, rounded to the frame grid."""
    return [
        (round((s - T0).total_seconds(), 2), round((e - T0).total_seconds(), 2))
        for s, e in result.voices[label]
    ]


def test_one_speaker_is_one_voice(engine: StandaloneDiarizer) -> None:
    clip = AudioClip(_tone(300, 6.0), T0)

    result = engine.diarize([clip])

    assert list(result.voices) == ["A"]
    start, end = _seconds(result, "A")[0]
    assert start == pytest.approx(0.0, abs=0.1)
    assert end == pytest.approx(6.0, abs=0.1)


def test_two_speakers_in_one_clip_become_two_voices(engine: StandaloneDiarizer) -> None:
    clip = AudioClip(np.concatenate([_tone(300, 4.0), _silence(0.5), _tone(2000, 4.0)]), T0)

    result = engine.diarize([clip])

    assert sorted(result.voices) == ["A", "B"]
    assert _seconds(result, "A")[0][0] == pytest.approx(0.0, abs=0.1)
    assert _seconds(result, "B")[0][0] == pytest.approx(4.5, abs=0.15)


def test_voice_a_is_whoever_speaks_first(engine: StandaloneDiarizer) -> None:
    clip = AudioClip(np.concatenate([_tone(2000, 4.0), _silence(0.5), _tone(300, 4.0)]), T0)

    result = engine.diarize([clip])

    assert _seconds(result, "A")[0][0] == pytest.approx(0.0, abs=0.1)


def test_spans_are_absolute_across_clips(engine: StandaloneDiarizer) -> None:
    """The whole point of the artifact: one identity's WAVs join the transcript
    on session time, so the engine must never emit clip-relative offsets."""
    later = T0 + timedelta(minutes=5)
    clips = [AudioClip(_tone(300, 4.0), T0), AudioClip(_tone(300, 4.0), later)]

    result = engine.diarize(clips)

    assert list(result.voices) == ["A"]
    spans = result.voices["A"]
    assert len(spans) == 2
    assert (spans[1][0] - later).total_seconds() == pytest.approx(0.0, abs=0.1)


def test_one_speaker_across_two_clips_stays_one_voice(engine: StandaloneDiarizer) -> None:
    """Clustering runs over the whole session at once (ADR-0021) — per-WAV runs
    would label the same human differently in each WAV."""
    clips = [
        AudioClip(_tone(300, 4.0), T0),
        AudioClip(_tone(2000, 4.0), T0 + timedelta(seconds=10)),
        AudioClip(_tone(300, 4.0), T0 + timedelta(seconds=20)),
    ]

    result = engine.diarize(clips)

    assert sorted(result.voices) == ["A", "B"]
    assert len(result.voices["A"]) == 2, "the same speaker got a new Voice in the later clip"


def test_silence_produces_no_voices(engine: StandaloneDiarizer) -> None:
    result = engine.diarize([AudioClip(_silence(5.0), T0)])

    assert result.voices == {}


def test_no_clips_produces_no_voices(engine: StandaloneDiarizer) -> None:
    assert engine.diarize([]).voices == {}


def test_a_speech_region_too_short_to_embed_is_skipped(engine: StandaloneDiarizer) -> None:
    """Under half a window the embedding is mostly padding-grade noise, and a
    junk vector clusters into a Voice of its own."""
    clip = AudioClip(np.concatenate([_tone(300, 0.4), _silence(1.0), _tone(300, 4.0)]), T0)

    result = engine.diarize([clip])

    assert list(result.voices) == ["A"]
    assert len(result.voices["A"]) == 1, "the sub-window blip became its own span"


def test_spans_tile_a_region_without_overlapping(engine: StandaloneDiarizer) -> None:
    """Windows overlap by half; spans must not. An overlap would make the
    merge-time join ambiguous exactly where the turn changes."""
    clip = AudioClip(np.concatenate([_tone(300, 4.0), _tone(2000, 4.0)]), T0)

    result = engine.diarize([clip])

    spans = sorted(s for voice in result.voices.values() for s in voice)
    for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
        assert start >= end, "two Voices claim the same instant"
        assert (start - end).total_seconds() <= FRAME_SHIFT_S, "a gap opened inside one speech region"


def test_a_span_round_trips_through_the_sidecar_format(engine: StandaloneDiarizer) -> None:
    """Spans are stored as ISO strings and read back by `text.parse_iso`, which
    stamps a naive one as UTC. So a clip started in local time comes back hours
    off with nothing raising — the round trip is what makes that visible."""
    result = engine.diarize([AudioClip(_tone(300, 4.0), T0)])

    start, end = result.voices["A"][0]

    assert parse_iso(start.isoformat()) == start
    assert parse_iso(end.isoformat()) == end


def test_the_engine_is_named_on_the_result(engine: StandaloneDiarizer) -> None:
    result = engine.diarize([AudioClip(_tone(300, 4.0), T0)])

    assert result.engine == "standalone"
    assert result.took_ms >= 0
