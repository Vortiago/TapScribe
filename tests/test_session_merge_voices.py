"""`merge_session` re-keys a diarized tap's segments to `slug#<voice>`.

The join is on absolute time, so the per-WAV cache is untouched and
re-diarizing never re-pays transcription (ADR-0021).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from wav_builders import seed_wav  # type: ignore[import-not-found]

from tapscribe import voices
from tapscribe.roster import record_occurrence
from tapscribe.session_merge import merge_session, select_session_wavs
from tapscribe.transcribers.base import TranscriptionResult, TranscriptionSegment, Word
from tapscribe.wav_cache import cached_transcribe

BASE = datetime(2026, 5, 12, 9, 19, 55, tzinfo=UTC)
SYSAUDIO_IDENTITY = "tray-system-audio-0011223344"
SLUG = "sysaudio"


def _wav_name(when: datetime, speaker: str, utt: str) -> str:
    return f"{when.strftime('%Y-%m-%dT%H-%M-%SZ')}_{speaker}_id01_{utt}.wav"


class _OneSegment:
    """One 0–6 s segment per WAV, optionally with word timestamps."""

    name = "fake"
    backend = "fake-backend"
    device = "test-device"
    model_name = "fake-model"

    def __init__(self, text: str, words: tuple[Word, ...] | None = None) -> None:
        self.text = text
        self.words = words

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
            segments=(TranscriptionSegment(start=0.0, end=6.0, text=self.text, words=self.words),),
            initial_prompt_used="",
            hotwords_used="",
            quality_settings={},
        )


def _seed_one(session_dir: Path, transcriber) -> Path:
    wav = seed_wav(session_dir / _wav_name(BASE, SLUG, "u00000000"))
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


def test_undiarized_session_keeps_the_plain_slug(tmp_path: Path) -> None:
    """No sidecar → byte-identical to the pre-ADR-0021 output."""
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    _seed_one(session_dir, _OneSegment("hello"))

    transcript = merge_session(select_session_wavs(session_dir))

    assert [s.speaker for s in transcript.segments] == [SLUG]


def test_diarized_tap_rekeys_its_segments_to_the_voice(tmp_path: Path) -> None:
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    _seed_one(session_dir, _OneSegment("hello"))
    _record(session_dir, {"A": [(0.0, 10.0)]})

    transcript = merge_session(select_session_wavs(session_dir))

    assert [s.speaker for s in transcript.segments] == [f"{SLUG}#A"]
    assert list(transcript.speakers) == [f"{SLUG}#A"]


def test_a_segment_crossing_a_voice_change_splits(tmp_path: Path) -> None:
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    words = (
        Word(start=0.0, end=1.0, word="alpha", prob=0.9),
        Word(start=1.0, end=2.0, word="beta", prob=0.9),
        Word(start=4.0, end=5.0, word="gamma", prob=0.9),
        Word(start=5.0, end=6.0, word="delta", prob=0.9),
    )
    _seed_one(session_dir, _OneSegment("alpha beta gamma delta", words))
    _record(session_dir, {"A": [(0.0, 3.0)], "B": [(3.0, 10.0)]})

    transcript = merge_session(select_session_wavs(session_dir))

    assert [s.speaker for s in transcript.segments] == [f"{SLUG}#A", f"{SLUG}#B"]
    assert [s.text for s in transcript.segments] == ["alpha beta", "gamma delta"]
    assert [s.source_wav for s in transcript.segments] == [
        transcript.segments[0].source_wav,
        transcript.segments[0].source_wav,
    ], "both halves still point at the WAV they came from"


def test_voices_for_another_identity_do_not_touch_this_tap(tmp_path: Path) -> None:
    """The sidecar is keyed by FULL identity; the Roster is what maps this
    WAV's slug onto it. A stranger's entry must not leak across."""
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    _seed_one(session_dir, _OneSegment("hello"))
    voices.record_voices(
        session_dir,
        identity="someone-else-entirely",
        run_id="run-1",
        spans={"A": [(BASE, BASE + timedelta(seconds=10))]},
    )

    transcript = merge_session(select_session_wavs(session_dir))

    assert [s.speaker for s in transcript.segments] == [SLUG]
