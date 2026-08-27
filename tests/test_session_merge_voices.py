"""`merge_session` re-keys a diarized tap's segments to `slug#<voice>`.

The join is on absolute time, so the per-WAV cache is untouched and
re-diarizing never re-pays transcription (ADR-0021).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from wav_builders import seed_wav  # type: ignore[import-not-found]

from tapscribe import voices
from tapscribe.roster import record_occurrence
from tapscribe.session_merge import merge_session, select_session_wavs
from tapscribe.transcribers.base import TranscriptionResult, TranscriptionSegment, Word
from tapscribe.wav_cache import cached_transcribe

BASE = datetime(2026, 5, 12, 9, 19, 55, tzinfo=UTC)
SYSAUDIO_IDENTITY = "tray-system-audio-0011223344"
SLUG = "sysaudio"


class _OneSegment:
    """One 0–6 s segment per WAV, optionally with word timestamps."""

    name = "fake"
    backend = "fake-backend"
    device = "test-device"
    model_name = "fake-model"

    def __init__(
        self, text: str, words: tuple[Word, ...] | None = None, avg_logprob: float | None = None
    ) -> None:
        self.text = text
        self.words = words
        self.avg_logprob = avg_logprob

    def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None):  # noqa: ARG002
        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            language="en",
            language_probability=1.0,
            duration=6.0,
            text=self.text,
            segments=(
                TranscriptionSegment(
                    start=0.0, end=6.0, text=self.text, words=self.words, avg_logprob=self.avg_logprob
                ),
            ),
            initial_prompt_used="",
            hotwords_used="",
            quality_settings={},
        )


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    d = tmp_path / "s"
    d.mkdir()
    return d


def _seed_one(session_dir: Path, transcriber) -> Path:
    stamp = BASE.strftime("%Y-%m-%dT%H-%M-%SZ")
    wav = seed_wav(session_dir / f"{stamp}_{SLUG}_id01_u00000000.wav")
    cached_transcribe(wav, transcriber, initial_prompt=None, hotwords=None, hallucination_rules=[])
    record_occurrence(
        session_dir, identity=SYSAUDIO_IDENTITY, name="System audio", recorded=True, wav=wav.name
    )
    return wav


def _record(session_dir: Path, spans: dict[str, list[tuple[float, float]]], run_id: str = "run-1") -> None:
    voices.record_voices(
        session_dir,
        identity=SYSAUDIO_IDENTITY,
        run_id=run_id,
        spans={
            label: [(BASE + timedelta(seconds=s), BASE + timedelta(seconds=e)) for s, e in windows]
            for label, windows in spans.items()
        },
    )


def test_undiarized_session_keeps_the_plain_slug(session_dir: Path) -> None:
    """No sidecar → byte-identical to the pre-ADR-0021 output."""
    _seed_one(session_dir, _OneSegment("hello"))

    transcript = merge_session(select_session_wavs(session_dir))

    assert [s.speaker for s in transcript.segments] == [SLUG]


def test_diarized_tap_rekeys_its_segments_to_the_voice(session_dir: Path) -> None:
    _seed_one(session_dir, _OneSegment("hello"))
    _record(session_dir, {"A": [(0.0, 10.0)]})

    transcript = merge_session(select_session_wavs(session_dir))

    assert [s.speaker for s in transcript.segments] == [f"{SLUG}#A"]
    assert list(transcript.speakers) == [f"{SLUG}#A"]


def test_a_segment_crossing_a_voice_change_splits(session_dir: Path) -> None:
    words = (
        Word(start=0.0, end=1.0, word="alpha", prob=0.9),
        Word(start=1.0, end=2.0, word="beta", prob=0.9),
        Word(start=4.0, end=5.0, word="gamma", prob=0.9),
        Word(start=5.0, end=6.0, word="delta", prob=0.9),
    )
    wav = _seed_one(session_dir, _OneSegment("alpha beta gamma delta", words))
    _record(session_dir, {"A": [(0.0, 3.0)], "B": [(3.0, 10.0)]})

    transcript = merge_session(select_session_wavs(session_dir))

    assert [s.speaker for s in transcript.segments] == [f"{SLUG}#A", f"{SLUG}#B"]
    assert [s.text for s in transcript.segments] == ["alpha beta", "gamma delta"]
    assert [s.source_wav for s in transcript.segments] == [wav.name, wav.name], (
        "both halves still point at the WAV they came from"
    )


def test_voices_for_another_identity_do_not_touch_this_tap(session_dir: Path) -> None:
    """The sidecar is keyed by FULL identity; the Roster is what maps this
    WAV's slug onto it. A stranger's entry must not leak across."""
    _seed_one(session_dir, _OneSegment("hello"))
    voices.record_voices(
        session_dir,
        identity="someone-else-entirely",
        run_id="run-1",
        spans={"A": [(BASE, BASE + timedelta(seconds=10))]},
    )

    transcript = merge_session(select_session_wavs(session_dir))

    assert [s.speaker for s in transcript.segments] == [SLUG]


def test_two_taps_sharing_a_display_name_stay_undiarized(session_dir: Path) -> None:
    """Both identities' Voices are refused rather than unioned, so segments keep
    the plain slug — wrong attribution is worse than none (#440)."""
    other = "tray-second-machine-99"
    _seed_one(session_dir, _OneSegment("hello"))
    stamp = BASE.strftime("%Y-%m-%dT%H-%M-%SZ")
    # A second identity recording under the SAME display name, hence same slug.
    seed_wav(session_dir / f"{stamp}_{SLUG}_id02_u00000001.wav")
    record_occurrence(
        session_dir,
        identity=other,
        name="System audio",
        recorded=True,
        wav=f"{stamp}_{SLUG}_id02_u00000001.wav",
    )
    _record(session_dir, {"A": [(0.0, 10.0)]})
    voices.record_voices(
        session_dir,
        identity=other,
        run_id="run-1",
        spans={"A": [(BASE, BASE + timedelta(seconds=10))]},
    )

    transcript = merge_session(select_session_wavs(session_dir))

    assert {s.speaker for s in transcript.segments} == {SLUG}, (
        "an ambiguous slug must attribute to nobody, not to whichever identity sorted first"
    )


# ---- aggregates must not count pieces (#441) -------------------------------

_UNCERTAIN = -1.0  # below _LOW_CONFIDENCE_LOGPROB_THRESHOLD


def _uncertain_words() -> tuple[Word, ...]:
    return (
        Word(start=0.0, end=1.0, word="alpha", prob=0.9),
        Word(start=1.0, end=2.0, word="beta", prob=0.9),
        Word(start=4.0, end=5.0, word="gamma", prob=0.9),
        Word(start=5.0, end=6.0, word="delta", prob=0.9),
    )


def test_one_uncertain_segment_crossing_two_voices_counts_once(session_dir: Path) -> None:
    """Counting pieces made the quality badge grow with how WELL diarization
    worked, and move on a re-diarize that changed no audio."""
    _seed_one(
        session_dir,
        _OneSegment("alpha beta gamma delta", _uncertain_words(), avg_logprob=_UNCERTAIN),
    )
    _record(session_dir, {"A": [(0.0, 3.0)], "B": [(3.0, 10.0)]})

    transcript = merge_session(select_session_wavs(session_dir))

    assert len(transcript.segments) == 2, "the segment did split"
    assert transcript.low_confidence_count == 1, "but it is ONE uncertain decode"


def test_a_split_segment_contributes_its_whole_duration_to_speaking_seconds(session_dir: Path) -> None:
    """Word-run bounds exclude the pause where the speaker changed, so a split
    segment used to contribute less than its own duration — understating
    diarized speakers against undiarized ones in the same session."""
    _seed_one(session_dir, _OneSegment("alpha beta gamma delta", _uncertain_words()))
    _record(session_dir, {"A": [(0.0, 3.0)], "B": [(3.0, 10.0)]})

    transcript = merge_session(select_session_wavs(session_dir))

    assert sum(transcript.speaking_seconds.values()) == 6.0, "the full 0-6 s segment"
    assert transcript.speaking_seconds == {f"{SLUG}#A": 3.0, f"{SLUG}#B": 3.0}
