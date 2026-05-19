"""Tests for tapscribe.wav_cache — the multi-transcript per-WAV cache."""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

from tapscribe.transcribers.base import TranscriptionResult, TranscriptionSegment
from tapscribe.wav_cache import (
    CachedTranscription,
    cached_transcribe,
    read_all_cached,
    read_cached,
    set_primary_transcript,
)

SAMPLE_RATE = 16000


def _wav(path: Path) -> Path:
    samples = np.tile(np.array([8000, -8000], dtype=np.int16), SAMPLE_RATE // 2)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())
    return path


class _StubTranscriber:
    name = "fake"
    backend = "fake-backend"
    device = "test-device"
    model_name = "fake-model"
    call_count = 0

    def transcribe(self, path, *, initial_prompt=None, hotwords=None):  # noqa: ARG002
        _StubTranscriber.call_count += 1
        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            language="en",
            language_probability=1.0,
            duration=1.0,
            text="stub transcript",
            segments=(TranscriptionSegment(start=0.0, end=1.0, text="stub transcript"),),
            initial_prompt_used=initial_prompt or "",
            hotwords_used=hotwords or "",
            quality_settings={},
        )


def test_read_cached_returns_none_when_sidecar_missing(tmp_path: Path):
    wav = _wav(tmp_path / "x.wav")
    assert read_cached(wav) is None


def test_cached_transcribe_runs_transcriber_on_miss_and_writes_sidecar(tmp_path: Path):
    wav = _wav(tmp_path / "x.wav")
    _StubTranscriber.call_count = 0
    cached = cached_transcribe(
        wav,
        _StubTranscriber(),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )
    assert isinstance(cached, CachedTranscription)
    assert _StubTranscriber.call_count == 1
    re_read = read_cached(wav)
    assert re_read is not None
    assert re_read.result.transcriber == "fake"
    assert re_read.result.model == "fake-model"
    assert re_read.result.backend == "fake-backend"


def test_sidecar_round_trips_backend_field(tmp_path: Path):
    """`backend` must persist through the JSON sidecar so the dashboard can
    render it for transcripts loaded after a restart."""
    wav = _wav(tmp_path / "x.wav")
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    re_read = read_cached(wav)
    assert re_read is not None
    assert re_read.result.backend == "fake-backend"


def test_legacy_sidecar_without_backend_field_loads_with_empty_backend(tmp_path: Path):
    """Older sidecars predate the backend field — they should still load
    (rather than crash) and surface backend as the empty string. The
    dashboard renders that as `?`."""
    wav = _wav(tmp_path / "x.wav")
    legacy = {
        "transcriber": "fake",
        # no "backend" key — this is the legacy shape
        "device": "test-device",
        "model": "fake-model",
        "language": "en",
        "language_probability": 1.0,
        "duration": 1.0,
        "segments": [],
        "text": "",
        "initial_prompt_used": "",
        "hotwords_used": "",
        "quality_settings": {},
        "suppressed_hallucinations": [],
        "transcribed_at": "2026-05-01T00:00:00+00:00",
        "transcribe_ms": 10,
        "source": "original",
        "speaker_name": "",
        "wav_size": wav.stat().st_size,
        "wav_mtime_ns": wav.stat().st_mtime_ns,
    }
    wav.with_suffix(".json").write_text(json.dumps(legacy), encoding="utf-8")
    re_read = read_cached(wav)
    assert re_read is not None
    assert re_read.result.backend == ""


def test_cached_transcribe_returns_cached_without_calling_transcriber_on_hit(tmp_path: Path):
    wav = _wav(tmp_path / "x.wav")
    _StubTranscriber.call_count = 0
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert _StubTranscriber.call_count == 1
    # Second call — should hit cache
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert _StubTranscriber.call_count == 1


def test_cached_transcribe_runs_for_different_model_and_promotes_it_to_primary(tmp_path: Path):
    """Switching models on the same WAV produces a fresh transcribe and
    the freshly-written entry becomes the primary. The original entry
    is preserved alongside (see test_two_backends_cache_independently)."""
    wav = _wav(tmp_path / "x.wav")
    _StubTranscriber.call_count = 0
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert _StubTranscriber.call_count == 1

    class _OtherTranscriber(_StubTranscriber):
        model_name = "other-model"

    cached_transcribe(wav, _OtherTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert _StubTranscriber.call_count == 2
    primary = read_cached(wav)
    assert primary is not None
    assert primary.result.model == "other-model"


def test_cached_transcribe_force_bypasses_cache(tmp_path: Path):
    wav = _wav(tmp_path / "x.wav")
    _StubTranscriber.call_count = 0
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    cached_transcribe(
        wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[], force=True
    )
    assert _StubTranscriber.call_count == 2


def test_cached_transcribe_records_envelope_metadata(tmp_path: Path):
    """The sidecar carries write-time envelope on top of the
    TranscriptionResult fields. Verify the envelope is surfaced on the
    parsed dataclass."""
    wav = _wav(tmp_path / "2026-05-12T09-19-55Z_alice_id01_ut000001.wav")
    cached = cached_transcribe(
        wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[]
    )
    assert cached.transcribed_at is not None
    assert cached.transcribe_ms >= 0
    assert cached.source == "original"
    assert cached.speaker_name == "alice"
    assert cached.wav_start is not None
    assert cached.wav_start.isoformat().startswith("2026-05-12T09:19:55")


def test_read_cached_returns_cached_transcription_dataclass(tmp_path: Path):
    wav = _wav(tmp_path / "x.wav")
    written = cached_transcribe(
        wav,
        _StubTranscriber(),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )
    re_read = read_cached(wav)
    assert isinstance(re_read, CachedTranscription)
    assert re_read.result.transcriber == written.result.transcriber
    assert re_read.transcribe_ms == written.transcribe_ms
    assert re_read.source == "original"


def test_corrupt_sidecar_treated_as_cache_miss(tmp_path: Path):
    wav = _wav(tmp_path / "x.wav")
    wav.with_suffix(".json").write_text("not valid JSON{{", encoding="utf-8")
    _StubTranscriber.call_count = 0
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert _StubTranscriber.call_count == 1  # transcriber WAS called despite the file existing


def test_cached_transcribe_re_runs_when_wav_was_rewritten(tmp_path: Path):
    """Resume path rewrites the same WAV in place with appended audio. The
    model name didn't change, but the bytes did — the cache must invalidate
    on size/mtime mismatch or merge_session returns the pre-resume transcript.
    """
    wav = _wav(tmp_path / "x.wav")
    _StubTranscriber.call_count = 0
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert _StubTranscriber.call_count == 1

    # Append-in-place: simulate the resume path. Bytes grow; mtime advances
    # naturally because the second write happens after the first.
    samples = np.tile(np.array([8000, -8000], dtype=np.int16), SAMPLE_RATE // 2)
    with wave.open(str(wav), "rb") as r:
        existing = r.readframes(r.getnframes())
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(existing + samples.tobytes())

    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert _StubTranscriber.call_count == 2, "rewritten WAV must miss the cache"


def test_cached_transcribe_treats_legacy_sidecar_without_fingerprint_as_miss(tmp_path: Path):
    """An older sidecar that predates the wav_size/wav_mtime_ns fields lands
    in the new code path with zero placeholders. Those won't equal the live
    stat values, so the next call re-transcribes. After that one rebuild
    the cache works normally."""
    wav = _wav(tmp_path / "x.wav")
    # Hand-craft a sidecar that looks like the pre-fingerprint format —
    # exactly what's on disk for any WAV transcribed before this change.
    legacy = {
        "transcriber": "fake",
        "device": "test-device",
        "model": "fake-model",
        "language": "en",
        "language_probability": 1.0,
        "duration": 1.0,
        "segments": [{"start": 0.0, "end": 1.0, "text": "stub transcript"}],
        "text": "stub transcript",
        "initial_prompt_used": "",
        "hotwords_used": "",
        "quality_settings": {},
        "suppressed_hallucinations": [],
        "transcribed_at": "2026-05-01T00:00:00+00:00",
        "transcribe_ms": 10,
        "source": "original",
        "speaker_name": "",
    }
    wav.with_suffix(".json").write_text(json.dumps(legacy), encoding="utf-8")
    _StubTranscriber.call_count = 0
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert _StubTranscriber.call_count == 1, "legacy sidecar without fingerprint should miss"
    # A second call with the freshly-written fingerprint must hit.
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert _StubTranscriber.call_count == 1


def test_cached_transcription_fingerprint_persists_to_sidecar(tmp_path: Path):
    """The fingerprint we wrote must round-trip through the JSON sidecar.
    Without this, every restart of the recorder would silently re-transcribe
    everything (legacy-fallback path) instead of hitting the cache."""
    wav = _wav(tmp_path / "x.wav")
    cached = cached_transcribe(
        wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[]
    )
    st = wav.stat()
    assert cached.wav_size == st.st_size
    assert cached.wav_mtime_ns == st.st_mtime_ns
    # And the on-disk sidecar round-trips it.
    re_read = read_cached(wav)
    assert re_read is not None
    assert re_read.wav_size == st.st_size
    assert re_read.wav_mtime_ns == st.st_mtime_ns


# ---------------------------------------------------------------------------
# Multi-transcript cache: many (backend, model) entries coexist for one WAV
# ---------------------------------------------------------------------------


class _StubByKey:
    """A stub Transcriber parameterized by (backend, model) with a
    per-instance call counter. Use this when a test needs two distinct
    transcribers and wants to assert each one independently."""

    name = "fake"
    device = "test-device"

    def __init__(self, *, backend: str, model: str, text: str | None = None) -> None:
        self.backend = backend
        self.model_name = model
        self.text = text or f"hello from {backend} {model}"
        self.call_count = 0

    def transcribe(self, path, *, initial_prompt=None, hotwords=None):  # noqa: ARG002
        self.call_count += 1
        return TranscriptionResult(
            transcriber=self.name,
            backend=self.backend,
            device=self.device,
            model=self.model_name,
            language="en",
            language_probability=1.0,
            duration=1.0,
            text=self.text,
            segments=(TranscriptionSegment(start=0.0, end=1.0, text=self.text),),
            initial_prompt_used=initial_prompt or "",
            hotwords_used=hotwords or "",
            quality_settings={},
        )


def test_two_backends_cache_independently(tmp_path: Path):
    """Writing for one (backend, model) doesn't invalidate the other's
    cached entry — A and B can coexist for the same WAV."""
    wav = _wav(tmp_path / "x.wav")
    a = _StubByKey(backend="faster-whisper", model="small.en")
    b = _StubByKey(backend="mlx-voxtral", model="voxtral-mini")

    cached_transcribe(wav, a, initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert a.call_count == 1

    cached_transcribe(wav, b, initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert b.call_count == 1

    # Second call with A — A's cache entry must still be there.
    cached_transcribe(wav, a, initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert a.call_count == 1, "writing B must not evict A's cached entry"


def test_read_cached_returns_most_recently_written_transcript_as_primary(tmp_path: Path):
    """Without an explicit primary set, the freshly-written entry wins —
    operators flipping models on the same WAV expect to see the
    just-produced result."""
    wav = _wav(tmp_path / "x.wav")
    a = _StubByKey(backend="faster-whisper", model="small.en", text="whisper text")
    b = _StubByKey(backend="mlx-voxtral", model="voxtral-mini", text="voxtral text")

    cached_transcribe(wav, a, initial_prompt=None, hotwords=None, hallucination_rules=[])
    cached_transcribe(wav, b, initial_prompt=None, hotwords=None, hallucination_rules=[])

    primary = read_cached(wav)
    assert primary is not None
    assert primary.result.backend == "mlx-voxtral"
    assert primary.result.model == "voxtral-mini"
    assert primary.result.text == "voxtral text"


def test_read_all_cached_returns_every_entry(tmp_path: Path):
    """`read_all_cached` surfaces every cached transcript for a WAV so
    the comparison UI can list them side by side."""
    wav = _wav(tmp_path / "x.wav")
    cached_transcribe(
        wav,
        _StubByKey(backend="faster-whisper", model="small.en", text="whisper"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )
    cached_transcribe(
        wav,
        _StubByKey(backend="mlx-voxtral", model="voxtral-mini", text="voxtral"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )

    everything = read_all_cached(wav)
    keys = {(c.result.backend, c.result.model) for c in everything}
    assert keys == {("faster-whisper", "small.en"), ("mlx-voxtral", "voxtral-mini")}


def test_read_all_cached_returns_empty_list_when_nothing_cached(tmp_path: Path):
    wav = _wav(tmp_path / "x.wav")
    assert read_all_cached(wav) == []


def test_set_primary_transcript_flips_the_pointer(tmp_path: Path):
    """After two writes, the explicit primary points the merge layer at
    whichever (backend, model) the operator picked, even if it wasn't
    the most recently written."""
    wav = _wav(tmp_path / "x.wav")
    cached_transcribe(
        wav,
        _StubByKey(backend="faster-whisper", model="small.en", text="whisper"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )
    cached_transcribe(
        wav,
        _StubByKey(backend="mlx-voxtral", model="voxtral-mini", text="voxtral"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )

    # Default primary is the newest write (voxtral). Flip it back to whisper.
    set_primary_transcript(wav, backend="faster-whisper", model="small.en")
    primary = read_cached(wav)
    assert primary is not None
    assert primary.result.backend == "faster-whisper"
    assert primary.result.text == "whisper"


def test_set_primary_transcript_raises_for_unknown_entry(tmp_path: Path):
    wav = _wav(tmp_path / "x.wav")
    cached_transcribe(
        wav,
        _StubByKey(backend="faster-whisper", model="small.en"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )
    import pytest

    with pytest.raises(FileNotFoundError):
        set_primary_transcript(wav, backend="mlx-voxtral", model="voxtral-mini")


def test_wav_rewrite_invalidates_each_backend_model_entry_independently(tmp_path: Path):
    """A WAV rewrite invalidates *every* cached transcript. Each entry
    carries its own fingerprint, so the next cached_transcribe call for
    that (backend, model) re-runs."""
    wav = _wav(tmp_path / "x.wav")
    a = _StubByKey(backend="faster-whisper", model="small.en")
    b = _StubByKey(backend="mlx-voxtral", model="voxtral-mini")
    cached_transcribe(wav, a, initial_prompt=None, hotwords=None, hallucination_rules=[])
    cached_transcribe(wav, b, initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert a.call_count == 1
    assert b.call_count == 1

    # Rewrite the WAV in place — simulates the resume path appending audio.
    samples = np.tile(np.array([8000, -8000], dtype=np.int16), SAMPLE_RATE // 2)
    with wave.open(str(wav), "rb") as r:
        existing = r.readframes(r.getnframes())
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(existing + samples.tobytes())

    cached_transcribe(wav, a, initial_prompt=None, hotwords=None, hallucination_rules=[])
    cached_transcribe(wav, b, initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert a.call_count == 2, "rewritten WAV must invalidate A's cached entry"
    assert b.call_count == 2, "rewritten WAV must invalidate B's cached entry"


def test_legacy_sidecar_migrates_into_new_layout_on_next_write(tmp_path: Path):
    """The first new-layout write for a WAV that has a legacy `<wav>.json`
    sidecar must migrate the legacy file into the new directory. This
    keeps the two formats from coexisting and lets read_all_cached see
    the previously-only entry."""
    wav = _wav(tmp_path / "x.wav")
    st = wav.stat()
    legacy = {
        "transcriber": "fake",
        "backend": "faster-whisper",
        "device": "cpu",
        "model": "small.en",
        "language": "en",
        "language_probability": 1.0,
        "duration": 1.0,
        "segments": [],
        "text": "from legacy file",
        "initial_prompt_used": "",
        "hotwords_used": "",
        "quality_settings": {},
        "suppressed_hallucinations": [],
        "transcribed_at": "2026-05-01T00:00:00+00:00",
        "transcribe_ms": 10,
        "source": "original",
        "speaker_name": "",
        "wav_size": st.st_size,
        "wav_mtime_ns": st.st_mtime_ns,
    }
    wav.with_suffix(".json").write_text(json.dumps(legacy), encoding="utf-8")

    # Trigger a write for a *different* (backend, model) to force the new layout.
    cached_transcribe(
        wav,
        _StubByKey(backend="mlx-voxtral", model="voxtral-mini", text="voxtral"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
    )

    # The legacy single-file sidecar is gone.
    assert not wav.with_suffix(".json").is_file(), (
        "legacy <wav>.json must be migrated, not left alongside the new directory"
    )
    # Both transcripts are now reachable via the public API.
    entries = read_all_cached(wav)
    backends_models = {(c.result.backend, c.result.model) for c in entries}
    assert backends_models == {("faster-whisper", "small.en"), ("mlx-voxtral", "voxtral-mini")}


def test_legacy_sidecar_still_serves_cache_hits_for_matching_backend_and_model(tmp_path: Path):
    """Tons of WAVs out there have a legacy `<wav>.json` sidecar. When
    cached_transcribe is called for the (backend, model) embedded in
    that legacy file with a matching fingerprint, it must hit — not
    re-run — so a system restart doesn't trigger a full re-transcribe."""
    wav = _wav(tmp_path / "x.wav")
    st = wav.stat()
    legacy = {
        "transcriber": "fake",
        "backend": "fake-backend",
        "device": "test-device",
        "model": "fake-model",
        "language": "en",
        "language_probability": 1.0,
        "duration": 1.0,
        "segments": [],
        "text": "from legacy file",
        "initial_prompt_used": "",
        "hotwords_used": "",
        "quality_settings": {},
        "suppressed_hallucinations": [],
        "transcribed_at": "2026-05-01T00:00:00+00:00",
        "transcribe_ms": 10,
        "source": "original",
        "speaker_name": "",
        "wav_size": st.st_size,
        "wav_mtime_ns": st.st_mtime_ns,
    }
    wav.with_suffix(".json").write_text(json.dumps(legacy), encoding="utf-8")

    # read_cached returns the legacy entry.
    re_read = read_cached(wav)
    assert re_read is not None
    assert re_read.result.text == "from legacy file"

    # cached_transcribe for the same (backend, model) hits the cache.
    stub = _StubByKey(backend="fake-backend", model="fake-model")
    cached_transcribe(wav, stub, initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert stub.call_count == 0, "legacy sidecar must satisfy a cache hit"
