"""The engine over real voices and the real model — does it actually separate
two humans, and does it leave one human alone.

A wrong fbank, a wrong CMN or a wrong threshold still returns plausible vectors,
and a test that only counts Voices goes green on an engine that is useless on a
same-channel meeting. So these assert SPANS: where the engine says the turn
changed, which is what `voice_join` consumes.

`marlene-nb` and `solen-da` are both studio narrations — channel-matched, so
separating them is a statement about the speakers and not about the recording
chain. Runs wherever the fetched model is present; CI's `diarize` lane fetches it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from tapscribe.diarizers import model as diarize_model
from tapscribe.diarizers.base import AudioClip
from tapscribe.diarizers.embed import CampPlusEmbedder
from tapscribe.diarizers.standalone import StandaloneDiarizer
from tapscribe.vad import load_model, speech_timestamps
from tests.fixtures.diarize import read_fixture_wav

pytestmark = [
    pytest.mark.real_audio,
    pytest.mark.skipif(
        not diarize_model.model_present(),
        reason="run `python -m tapscribe.diarizers.model` to fetch the embedding model",
    ),
]

RATE = 16000
T0 = datetime(2026, 8, 20, 9, 0, 0, tzinfo=UTC)
TURN_S, GAP_S = 3.0, 0.5


@pytest.fixture(scope="module")
def engine() -> StandaloneDiarizer:
    return StandaloneDiarizer(CampPlusEmbedder.load(), vad=load_model())


@pytest.fixture(scope="module")
def speech() -> dict[str, np.ndarray]:
    """Each fixture's speech with its pauses removed, so a turn built from it is
    wall-to-wall voice and a boundary assertion means something."""
    vad = load_model()
    out = {}
    for name in ("marlene-nb", "solen-da"):
        sig = read_fixture_wav(name)
        out[name] = np.concatenate([sig[r["start"] : r["end"]] for r in speech_timestamps(sig, vad)])
    return out


def _turns(speech: dict[str, np.ndarray], order: list[str], *, gap_s: float) -> np.ndarray:
    """`order` of speaker keys → one clip, each taking the next unused TURN_S of
    its own speech so no turn repeats audio."""
    gap = np.zeros(int(gap_s * RATE), dtype=np.float32)
    taken: dict[str, int] = {}
    pieces = []
    for i, who in enumerate(order):
        at = taken.get(who, 0)
        taken[who] = at + int(TURN_S * RATE)
        pieces.append(speech[who][at : at + int(TURN_S * RATE)])
        if gap_s and i < len(order) - 1:
            pieces.append(gap)
    return np.concatenate(pieces)


def _flat(result) -> list[tuple[str, float, float]]:
    """`[(label, start_s, end_s)]` in time order, offsets from T0."""
    spans = [
        (label, (s - T0).total_seconds(), (e - T0).total_seconds())
        for label, windows in result.voices.items()
        for s, e in windows
    ]
    return sorted(spans, key=lambda s: s[1])


def test_five_turns_recover_two_voices_at_the_right_instants(engine, speech) -> None:
    """The real test. Two studio voices, five short turns, and the boundaries
    have to land where the turns actually change."""
    order = ["marlene-nb", "solen-da", "marlene-nb", "solen-da", "marlene-nb"]
    clip = AudioClip(_turns(speech, order, gap_s=GAP_S), T0)

    result = engine.diarize([clip])

    assert sorted(result.voices) == ["A", "B"]
    spans = _flat(result)
    assert len(spans) == 5, f"expected one span per turn, got {spans}"
    assert [s[0] for s in spans] == ["A", "B", "A", "B", "A"], "the turns did not alternate"
    for i, (_, start, end) in enumerate(spans):
        want = i * (TURN_S + GAP_S)
        assert start == pytest.approx(want, abs=0.25), f"turn {i} starts at {start}, not {want}"
        assert end == pytest.approx(want + TURN_S, abs=0.25), f"turn {i} ends at {end}"


def test_one_speaker_across_five_turns_stays_one_voice(engine, speech) -> None:
    """The dangerous direction: a false split puts a stranger's name on half of
    someone's words. Same audio shape as above, one human."""
    order = ["marlene-nb"] * 3
    clip = AudioClip(_turns(speech, order, gap_s=GAP_S), T0)

    result = engine.diarize([clip])

    assert list(result.voices) == ["A"], f"one speaker split into {sorted(result.voices)}"


def test_a_turn_change_with_no_pause_still_splits(engine, speech) -> None:
    """No gap, so the VAD hands the engine one region and the split has to come
    from the embeddings — with windows straddling both boundaries."""
    order = ["marlene-nb", "solen-da", "marlene-nb"]
    clip = AudioClip(_turns(speech, order, gap_s=0.0), T0)

    result = engine.diarize([clip])

    assert sorted(result.voices) == ["A", "B"]
    truth = [(who, i * TURN_S, (i + 1) * TURN_S) for i, who in enumerate(order)]
    by_label = {"A": "marlene-nb", "B": "solen-da"}
    right = sum(
        max(0.0, min(end, t_end) - max(start, t_start))
        for label, start, end in _flat(result)
        for who, t_start, t_end in truth
        if by_label[label] == who
    )
    assert right / (len(order) * TURN_S) > 0.85, "under 85% of the audio landed on the right Voice"


def test_a_single_speaker_clip_is_one_voice(engine) -> None:
    result = engine.diarize([AudioClip(read_fixture_wav("solen-da"), T0)])

    assert list(result.voices) == ["A"]


def test_silence_is_no_voices(engine) -> None:
    result = engine.diarize([AudioClip(np.zeros(5 * RATE, dtype=np.float32), T0)])

    assert result.voices == {}
