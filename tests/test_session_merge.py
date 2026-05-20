"""Tests for merge_session — the pure merger that reads per-WAV sidecars
and produces a `SessionTranscript`.

We build a tmpdir session with two WAVs and pre-write their primary
sidecars via `cached_transcribe` so the test exercises the read-and-build
path end-to-end without spinning up any real model.
"""

from __future__ import annotations

import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from tapscribe.session_merge import (
    SessionTranscript,
    merge_session,
    select_session_wavs,
)
from tapscribe.transcribers.base import TranscriptionResult, TranscriptionSegment
from tapscribe.wav_cache import cached_transcribe, set_primary_transcript

SAMPLE_RATE = 16000


def _wav(path: Path) -> Path:
    samples = np.tile(np.array([8000, -8000], dtype=np.int16), SAMPLE_RATE // 2)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())
    return path


def _wav_name(when: datetime, speaker: str, utt: str) -> str:
    return f"{when.strftime('%Y-%m-%dT%H-%M-%SZ')}_{speaker}_id01_{utt}.wav"


class _FixedTranscriber:
    """Returns one canned segment per WAV with `text` set to `wav.name`
    (handy for asserting which WAVs the merge pulled in)."""

    name = "fake"
    backend = "fake-backend"
    device = "test-device"
    model_name = "fake-model"

    def transcribe(self, path, *, initial_prompt=None, hotwords=None):  # noqa: ARG002
        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            language="en",
            language_probability=1.0,
            duration=1.0,
            text=f"hello from {path.name}",
            segments=(TranscriptionSegment(start=0.0, end=1.0, text=f"hello from {path.name}"),),
            initial_prompt_used="",
            hotwords_used="",
            quality_settings={},
        )


def _seed(session_dir: Path, n: int = 2, speakers: list[str] | None = None) -> list[Path]:
    base = datetime(2026, 5, 12, 9, 19, 55, tzinfo=timezone.utc)
    speakers = speakers or ["alice"] * n
    paths = []
    for i in range(n):
        wav = _wav(session_dir / _wav_name(base + timedelta(seconds=10 * i), speakers[i], f"u{i:08d}"))
        paths.append(wav)
    return paths


def test_merge_empty_selection_returns_zero_wav_transcript(tmp_path: Path):
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    selection = select_session_wavs(session_dir)
    transcript = merge_session(selection)
    assert isinstance(transcript, SessionTranscript)
    assert transcript.wav_count == 0
    assert transcript.segments == ()


def test_merge_uses_cached_per_wav_sidecars(tmp_path: Path):
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    wavs = _seed(session_dir, n=2)
    transcriber = _FixedTranscriber()
    for wav in wavs:
        cached_transcribe(wav, transcriber, initial_prompt=None, hotwords=None, hallucination_rules=[])

    selection = select_session_wavs(session_dir)
    transcript = merge_session(selection)
    assert transcript.wav_count == 2
    assert len(transcript.segments) == 2
    # Both wavs contributed text, sorted by absolute start time
    assert transcript.segments[0].text == f"hello from {wavs[0].name}"
    assert transcript.segments[1].text == f"hello from {wavs[1].name}"


def test_merge_records_skipped_no_cache_when_sidecar_missing(tmp_path: Path):
    """Per the design: merge_session never transcribes. WAVs without a
    sidecar are skipped and surfaced via `skipped_no_cache`."""
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    wavs = _seed(session_dir, n=2)
    # Only one WAV gets cached; the other is left without a sidecar.
    cached_transcribe(
        wavs[0], _FixedTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[]
    )

    selection = select_session_wavs(session_dir)
    transcript = merge_session(selection)
    assert transcript.wav_count == 1  # only the cached one contributed
    assert wavs[1].name in transcript.skipped_no_cache


def test_merge_attaches_absolute_timestamps_to_segments(tmp_path: Path):
    """Each segment's absolute time is wav_start + segment_offset, surfaced
    as a tz-aware datetime on the merged SessionSegment."""
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    wavs = _seed(session_dir, n=1)
    cached_transcribe(
        wavs[0], _FixedTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[]
    )

    selection = select_session_wavs(session_dir)
    transcript = merge_session(selection)
    seg = transcript.segments[0]
    # WAV starts at base; segment offset 0 → abs_start equals wav_start
    assert seg.abs_start == datetime(2026, 5, 12, 9, 19, 55, tzinfo=timezone.utc)
    assert seg.abs_end == datetime(2026, 5, 12, 9, 19, 56, tzinfo=timezone.utc)


def test_merge_computes_speaking_seconds_as_dict_keyed_by_speaker(tmp_path: Path):
    """Wire-shape cleanup: speaking_seconds is a dict, not parallel arrays."""
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    wavs = _seed(session_dir, n=3, speakers=["alice", "bob", "alice"])
    for wav in wavs:
        cached_transcribe(
            wav, _FixedTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[]
        )

    selection = select_session_wavs(session_dir)
    transcript = merge_session(selection)
    assert sorted(transcript.speakers) == ["alice", "bob"]
    assert isinstance(transcript.speaking_seconds, dict)
    # Each WAV is 1s; alice spoke in 2 of 3, bob in 1.
    assert transcript.speaking_seconds["alice"] == 2.0
    assert transcript.speaking_seconds["bob"] == 1.0


def test_merge_carries_suppressed_segments_with_absolute_timestamps(tmp_path: Path):
    """suppressed segments from per-WAV cache should appear in the
    session-level suppressed list with absolute timestamps."""
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    base = datetime(2026, 5, 12, 9, 19, 55, tzinfo=timezone.utc)
    wav = _wav(session_dir / _wav_name(base, "alice", "u00000001"))

    class _SuppressingTranscriber(_FixedTranscriber):
        def transcribe(self, path, *, initial_prompt=None, hotwords=None):  # noqa: ARG002
            return TranscriptionResult(
                transcriber=self.name,
                backend=self.backend,
                device=self.device,
                model=self.model_name,
                language="en",
                language_probability=1.0,
                duration=2.0,
                text="amara.org",
                segments=(TranscriptionSegment(start=0.0, end=2.0, text="amara.org"),),
                initial_prompt_used="",
                hotwords_used="",
                quality_settings={},
            )

    cached_transcribe(
        wav,
        _SuppressingTranscriber(),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[{"raw": "amara.org", "kind": "substr", "matcher": "amara.org"}],
    )

    selection = select_session_wavs(session_dir)
    transcript = merge_session(selection)
    assert transcript.segments == ()  # all suppressed
    assert len(transcript.suppressed) == 1
    assert transcript.suppressed[0].matched_rule == "amara.org"
    assert transcript.suppressed[0].abs_start == base


def test_merge_drops_abs_hms_field_from_wire_shape(tmp_path: Path):
    """Wire-shape cleanup: abs_hms is dropped — dashboards format from
    abs_start themselves. Verify by inspecting the SessionTranscript's
    to_dict() output."""
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    wavs = _seed(session_dir, n=1)
    cached_transcribe(
        wavs[0], _FixedTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[]
    )

    selection = select_session_wavs(session_dir)
    transcript = merge_session(selection)
    serialized = transcript.to_dict()
    assert serialized["segments"], "should have one segment"
    assert "abs_hms" not in serialized["segments"][0]
    # speaking_seconds is a dict on the wire too
    assert isinstance(serialized["speaking_seconds"], dict)


class _StubB(_FixedTranscriber):
    """A second canned transcriber with a different (backend, model) and
    a distinctive prefix on its text so merge tests can tell whose
    transcript landed where."""

    name = "fake"
    backend = "stub-b-backend"
    device = "test-device"
    model_name = "stub-b-model"

    def transcribe(self, path, *, initial_prompt=None, hotwords=None):  # noqa: ARG002
        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            language="en",
            language_probability=1.0,
            duration=1.0,
            text=f"FROM_B {path.name}",
            segments=(TranscriptionSegment(start=0.0, end=1.0, text=f"FROM_B {path.name}"),),
            initial_prompt_used="",
            hotwords_used="",
            quality_settings={},
        )


def test_merge_session_reads_the_primary_transcript_when_multiple_exist(tmp_path: Path):
    """When a WAV has multiple cached transcripts, merge_session must
    consume whichever one the primary pointer names — flipping the
    primary changes what the merged session contains."""
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    (wav,) = _seed(session_dir, n=1)

    # Two transcribes: A first, then B (so B is the default primary).
    cached_transcribe(wav, _FixedTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    cached_transcribe(wav, _StubB(), initial_prompt=None, hotwords=None, hallucination_rules=[])

    # Default: most-recent-written (B) is primary, so its text shows up.
    selection = select_session_wavs(session_dir)
    transcript = merge_session(selection)
    assert transcript.segments[0].text == f"FROM_B {wav.name}"
    assert transcript.backend == "stub-b-backend"
    assert transcript.model == "stub-b-model"

    # Flip primary back to A: merge now sees A's text.
    set_primary_transcript(wav, backend="fake-backend", model="fake-model")
    transcript = merge_session(select_session_wavs(session_dir))
    assert transcript.segments[0].text == f"hello from {wav.name}"
    assert transcript.backend == "fake-backend"
    assert transcript.model == "fake-model"


def test_merge_session_mixes_primaries_across_wavs(tmp_path: Path):
    """Different WAVs may have different primaries within one session.
    The merge layer picks each WAV's primary independently."""
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    wavs = _seed(session_dir, n=2)

    # Both WAVs get an A transcript and a B transcript.
    for wav in wavs:
        cached_transcribe(
            wav, _FixedTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[]
        )
        cached_transcribe(wav, _StubB(), initial_prompt=None, hotwords=None, hallucination_rules=[])

    # First WAV stays on B (default), second WAV flips back to A.
    set_primary_transcript(wavs[1], backend="fake-backend", model="fake-model")

    transcript = merge_session(select_session_wavs(session_dir))
    texts = [s.text for s in transcript.segments]
    assert texts == [f"FROM_B {wavs[0].name}", f"hello from {wavs[1].name}"]
