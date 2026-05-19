"""Tests for tapscribe.wav_cache — the per-WAV JSON sidecar."""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

from tapscribe.transcribers.base import TranscriptionResult, TranscriptionSegment
from tapscribe.wav_cache import (
    CachedTranscription,
    cached_transcribe,
    read_cached,
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

    def transcribe(
        self,
        path,  # noqa: ARG002
        *,
        initial_prompt=None,
        hotwords=None,
        source_lang=None,
        target_lang=None,
    ):
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
            source_language=source_lang or "",
            target_language=(target_lang or "") if (target_lang and target_lang != source_lang) else "",
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
    sidecar = wav.with_suffix(".json")
    assert sidecar.is_file()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["transcriber"] == "fake"
    assert data["model"] == "fake-model"
    assert data["backend"] == "fake-backend"


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


def test_cached_transcribe_re_runs_when_model_mismatched(tmp_path: Path):
    wav = _wav(tmp_path / "x.wav")
    # First call with stub (model_name="fake-model")
    _StubTranscriber.call_count = 0
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert _StubTranscriber.call_count == 1

    # Second call with a different model — cache should be invalidated
    class _OtherTranscriber(_StubTranscriber):
        model_name = "other-model"

    cached_transcribe(wav, _OtherTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert _StubTranscriber.call_count == 2
    sidecar = json.loads((wav.with_suffix(".json")).read_text(encoding="utf-8"))
    assert sidecar["model"] == "other-model"


def test_cached_transcribe_force_bypasses_cache(tmp_path: Path):
    wav = _wav(tmp_path / "x.wav")
    _StubTranscriber.call_count = 0
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    cached_transcribe(
        wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[], force=True
    )
    assert _StubTranscriber.call_count == 2


def test_cached_transcribe_records_envelope_metadata(tmp_path: Path):
    """The sidecar JSON carries write-time envelope on top of the
    TranscriptionResult fields. Verify the envelope keys exist."""
    wav = _wav(tmp_path / "2026-05-12T09-19-55Z_alice_id01_ut000001.wav")
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    data = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))
    assert "transcribed_at" in data
    assert "transcribe_ms" in data
    assert data["source"] == "original"
    assert data["speaker_name"] == "alice"
    assert data["wav_start"].startswith("2026-05-12T09:19:55")


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
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    sidecar = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))
    st = wav.stat()
    assert sidecar["wav_size"] == st.st_size
    assert sidecar["wav_mtime_ns"] == st.st_mtime_ns
