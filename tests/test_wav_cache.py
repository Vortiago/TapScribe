"""Tests for tapscribe.wav_cache — the multi-transcript per-WAV cache."""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np
import pytest
from wav_builders import seed_wav  # type: ignore[import-not-found]

from tapscribe.transcribers.base import TranscriptionResult, TranscriptionSegment
from tapscribe.wav_cache import (
    _PRIMARY_POINTER,
    CachedTranscription,
    _read_entry,
    cache_listing,
    cached_transcribe,
    read_all_cached,
    read_cached,
    read_primary_marker,
    read_primary_payload,
    set_primary_transcript,
    transcripts_dir,
)

SAMPLE_RATE = 16000


class _StubTranscriber:
    name = "fake"
    backend = "fake-backend"
    device = "test-device"
    model_name = "fake-model"
    call_count = 0

    def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None):  # noqa: ARG002
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
            # Real adapters echo the pin they decoded under; the cache compares
            # it against the caller's `source_lang` on the next call.
            source_language=source_lang or "",
            quality_settings={},
        )


def test_read_cached_returns_none_when_sidecar_missing(tmp_path: Path):
    wav = seed_wav(tmp_path / "x.wav")
    assert read_cached(wav) is None


def test_cached_transcribe_runs_transcriber_on_miss_and_writes_sidecar(tmp_path: Path):
    wav = seed_wav(tmp_path / "x.wav")
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


def test_cache_listing_includes_source(tmp_path: Path):
    """Each cache-listing entry must carry the `source` it was transcribed from
    ("original" | "stripped"). The dashboard's set-primary uses it to resolve
    the file's directory — a stripped clip lives in <session>/stripped/, so a
    listing that omitted source made the UI fall back to "original" and 404 the
    PUT (it looked for the clip in the originals dir)."""
    wav = seed_wav(tmp_path / "x.wav")
    cached_transcribe(
        wav,
        _StubTranscriber(),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
        source="stripped",
    )
    listing = cache_listing(wav)
    assert len(listing) == 1
    assert listing[0]["source"] == "stripped"


def test_cache_listing_legacy_sidecar_reports_source(tmp_path: Path):
    """The legacy single-`<wav>.json` path must also surface source so a
    pre-split-layout stripped clip's set-primary resolves correctly."""
    wav = seed_wav(tmp_path / "x.wav")
    legacy = {
        "transcriber": "fake",
        "backend": "fake-backend",
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
        "source": "stripped",
        "speaker_name": "",
    }
    wav.with_suffix(".json").write_text(json.dumps(legacy), encoding="utf-8")
    listing = cache_listing(wav)
    assert len(listing) == 1
    assert listing[0]["source"] == "stripped"


def test_sidecar_round_trips_backend_field(tmp_path: Path):
    """`backend` must persist through the JSON sidecar so the dashboard can
    render it for transcripts loaded after a restart."""
    wav = seed_wav(tmp_path / "x.wav")
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    re_read = read_cached(wav)
    assert re_read is not None
    assert re_read.result.backend == "fake-backend"


def test_legacy_sidecar_without_backend_field_loads_with_empty_backend(tmp_path: Path):
    """Older sidecars predate the backend field — they should still load
    (rather than crash) and surface backend as the empty string. The
    dashboard renders that as `?`."""
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "2026-05-12T09-19-55Z_alice_id01_ut000001.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
    wav.with_suffix(".json").write_text("not valid JSON{{", encoding="utf-8")
    _StubTranscriber.call_count = 0
    cached_transcribe(wav, _StubTranscriber(), initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert _StubTranscriber.call_count == 1  # transcriber WAS called despite the file existing


def test_cached_transcribe_re_runs_when_wav_was_rewritten(tmp_path: Path):
    """Resume path rewrites the same WAV in place with appended audio. The
    model name didn't change, but the bytes did — the cache must invalidate
    on size/mtime mismatch or merge_session returns the pre-resume transcript.
    """
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
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

    def __init__(
        self,
        *,
        backend: str,
        model: str,
        text: str | None = None,
        segment_texts: tuple[str, ...] = (),
    ) -> None:
        self.backend = backend
        self.model_name = model
        self.text = text or f"hello from {backend} {model}"
        # One segment carrying `text` unless the test wants several (the
        # hallucination-filter cases need a mix of keepers and matches).
        self.segment_texts = segment_texts or (self.text,)
        self.call_count = 0

    def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None):  # noqa: ARG002
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
            segments=tuple(
                TranscriptionSegment(start=float(i), end=float(i) + 1.0, text=t)
                for i, t in enumerate(self.segment_texts)
            ),
            initial_prompt_used=initial_prompt or "",
            hotwords_used=hotwords or "",
            source_language=source_lang or "",
            quality_settings={},
        )


def test_two_backends_cache_independently(tmp_path: Path):
    """Writing for one (backend, model) doesn't invalidate the other's
    cached entry — A and B can coexist for the same WAV."""
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
    assert read_all_cached(wav) == []


def test_set_primary_transcript_flips_the_pointer(tmp_path: Path):
    """After two writes, the explicit primary points the merge layer at
    whichever (backend, model) the operator picked, even if it wasn't
    the most recently written."""
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
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
    wav = seed_wav(tmp_path / "x.wav")
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


def test_primary_read_parses_at_most_the_primary_sidecar(tmp_path: Path, monkeypatch):
    """Hot-path ceiling: resolving/streaming the primary must parse only the
    primary sidecar, never every sibling. `read_primary_payload` bypasses the
    dataclass build entirely (0 `_read_entry` calls — it streams the raw dict),
    and `read_cached` parses exactly ONE sidecar (the primary). This is the
    HARM-layer pin the structural seam contract can't see: a future reroute of
    `_primary_sidecar_path` back through the parse-all `_resolve_sidecars`
    would re-parse every sibling here (3 / 4 instead of 0 / 1) and redden."""
    wc = sys.modules[_read_entry.__module__]

    wav = seed_wav(tmp_path / "x.wav")
    for i in range(3):
        cached_transcribe(
            wav,
            _StubByKey(backend=f"backend-{i}", model=f"model-{i}"),
            initial_prompt=None,
            hotwords=None,
            hallucination_rules=[],
        )
    assert len(read_all_cached(wav)) == 3

    calls = {"n": 0}
    real_read_entry = _read_entry

    def counting(path):
        calls["n"] += 1
        return real_read_entry(path)

    monkeypatch.setattr(wc, "_read_entry", counting)

    calls["n"] = 0
    payload = read_primary_payload(wav)
    assert isinstance(payload, dict)
    assert calls["n"] == 0, (
        "read_primary_payload must build no dataclass — parse-free resolve + one raw json.loads"
    )

    calls["n"] = 0
    primary = read_cached(wav)
    assert primary is not None
    assert calls["n"] == 1, "read_cached must parse only the primary sidecar, not every sibling"


def test_read_primary_payload_streams_incomplete_valid_json_primary(tmp_path: Path):
    """A primary sidecar that is valid JSON but fails the dataclass build
    (here: missing `device`/`transcriber`) must STILL stream its raw dict —
    `read_primary_payload` bypasses the dataclass on purpose so an older- or
    partially-written sidecar keeps showing on the dashboard. `read_cached`,
    which builds the dataclass, returns None for the same input — the
    documented asymmetry."""
    from tapscribe.wav_cache import _PRIMARY_POINTER, read_primary_payload, transcripts_dir

    wav = seed_wav(tmp_path / "x.wav")
    d = transcripts_dir(wav)
    d.mkdir(parents=True, exist_ok=True)
    incomplete = {"backend": "b", "model": "m", "transcribed_at": "2026-05-01T00:00:00+00:00"}
    (d / "b__m.json").write_text(json.dumps(incomplete), encoding="utf-8")
    (d / _PRIMARY_POINTER).write_text("b__m", encoding="utf-8")

    assert read_primary_payload(wav) == incomplete
    assert read_cached(wav) is None


# ---------------------------------------------------------------------------
# Match key: an entry written under one prompt / hotwords / language pin must
# not be served when the caller now wants another. These are the guarantees
# `cached_transcribe`'s docstring makes; without them the 5-clause match
# condition could collapse back to the size/mtime fingerprint with CI green.
# ---------------------------------------------------------------------------


def test_cached_transcribe_re_runs_when_initial_prompt_changes(tmp_path: Path):
    wav = seed_wav(tmp_path / "x.wav")
    stub = _StubByKey(backend="faster-whisper", model="small.en")

    cached_transcribe(wav, stub, initial_prompt="A", hotwords=None, hallucination_rules=[])
    assert stub.call_count == 1

    # Same prompt → cache hit (so the miss below can't be "always misses").
    cached_transcribe(wav, stub, initial_prompt="A", hotwords=None, hallucination_rules=[])
    assert stub.call_count == 1

    # Edited session-meta prompt → must re-run, not serve the stale transcript.
    cached_transcribe(wav, stub, initial_prompt="B", hotwords=None, hallucination_rules=[])
    assert stub.call_count == 2
    fresh = read_cached(wav)
    assert fresh is not None
    assert fresh.result.initial_prompt_used == "B"


def test_cached_transcribe_re_runs_when_hotwords_change(tmp_path: Path):
    wav = seed_wav(tmp_path / "x.wav")
    stub = _StubByKey(backend="faster-whisper", model="small.en")

    cached_transcribe(wav, stub, initial_prompt=None, hotwords="Kubernetes", hallucination_rules=[])
    assert stub.call_count == 1

    cached_transcribe(wav, stub, initial_prompt=None, hotwords="Kubernetes", hallucination_rules=[])
    assert stub.call_count == 1

    cached_transcribe(wav, stub, initial_prompt=None, hotwords="Kubernetes,Grafana", hallucination_rules=[])
    assert stub.call_count == 2
    fresh = read_cached(wav)
    assert fresh is not None
    assert fresh.result.hotwords_used == "Kubernetes,Grafana"


def test_cached_transcribe_re_runs_when_source_lang_changes(tmp_path: Path):
    """The language pin (ADR-0010) is part of the match key: an entry decoded
    as Norwegian must not be served when the operator re-pins to English."""
    wav = seed_wav(tmp_path / "x.wav")
    stub = _StubByKey(backend="faster-whisper", model="small.en")

    cached_transcribe(wav, stub, initial_prompt=None, hotwords=None, hallucination_rules=[], source_lang="no")
    assert stub.call_count == 1

    cached_transcribe(wav, stub, initial_prompt=None, hotwords=None, hallucination_rules=[], source_lang="no")
    assert stub.call_count == 1

    cached_transcribe(wav, stub, initial_prompt=None, hotwords=None, hallucination_rules=[], source_lang="en")
    assert stub.call_count == 2
    fresh = read_cached(wav)
    assert fresh is not None
    assert fresh.result.source_language == "en"


# ---------------------------------------------------------------------------
# Hallucination rules on a CACHE HIT — an edited rules file must change what
# the dashboard shows without re-running the model.
# ---------------------------------------------------------------------------

_AMARA_RULE = {"raw": "amara.org", "kind": "substr", "matcher": "amara.org"}


def _transcribe_two_segments(wav: Path, stub, rules: list) -> None:
    cached_transcribe(wav, stub, initial_prompt=None, hotwords=None, hallucination_rules=rules)


def test_cache_hit_applies_a_newly_added_hallucination_rule(tmp_path: Path):
    """Operator spots a hallucination, adds a rule, clicks Transcribe session
    WITHOUT force: every WAV is a cache hit, so the filter has to be re-applied
    over the stored raw result (segments + suppressed) — otherwise the merged
    transcript comes back byte-identical and the rule looks broken.

    No model run: the transcriber's call count must not move."""
    wav = seed_wav(tmp_path / "x.wav")
    stub = _StubByKey(
        backend="faster-whisper",
        model="small.en",
        segment_texts=("real speech here", "Subtitles by the Amara.org community"),
    )
    _transcribe_two_segments(wav, stub, [])
    assert stub.call_count == 1
    before = read_cached(wav)
    assert before is not None
    assert len(before.result.segments) == 2

    _transcribe_two_segments(wav, stub, [_AMARA_RULE])
    assert stub.call_count == 1, "re-filtering must not re-run the model"

    # The SIDECAR must change — session_merge re-reads it, so a return-value-
    # only fix would leave the merged transcript stale.
    after = read_cached(wav)
    assert after is not None
    assert [s.text for s in after.result.segments] == ["real speech here"]
    assert [s.text for s in after.result.suppressed_hallucinations] == [
        "Subtitles by the Amara.org community"
    ]
    assert after.result.suppressed_hallucinations[0].matched_rule == "amara.org"
    # The envelope is untouched — no model ran.
    assert after.transcribed_at == before.transcribed_at
    assert after.transcribe_ms == before.transcribe_ms


def test_cache_hit_restores_a_segment_when_its_rule_is_removed(tmp_path: Path):
    """The dual direction: deleting an over-eager rule must bring the segment
    back into `segments`, in TEMPORAL order, again without a model run."""
    wav = seed_wav(tmp_path / "x.wav")
    stub = _StubByKey(
        backend="faster-whisper",
        model="small.en",
        segment_texts=("Subtitles by the Amara.org community", "real speech here"),
    )
    _transcribe_two_segments(wav, stub, [_AMARA_RULE])
    assert stub.call_count == 1
    suppressed_first = read_cached(wav)
    assert suppressed_first is not None
    assert [s.text for s in suppressed_first.result.segments] == ["real speech here"]

    _transcribe_two_segments(wav, stub, [])
    assert stub.call_count == 1, "restoring a segment must not re-run the model"

    restored = read_cached(wav)
    assert restored is not None
    assert [s.text for s in restored.result.segments] == [
        "Subtitles by the Amara.org community",
        "real speech here",
    ], "the restored segment belongs at its own start time, not appended at the end"
    assert restored.result.suppressed_hallucinations == ()
    assert restored.result.segments[0].matched_rule is None, "stale rule annotation must be cleared"


def test_cache_hit_with_unchanged_rules_does_not_rewrite_the_entry(tmp_path: Path):
    """A plain re-run must stay a pure cache hit. The observable consequence
    of a needless rewrite is primary theft: `_write_entry` re-points `_primary`
    at the entry it writes, so an operator's pinned primary would silently flip
    on every unchanged re-transcribe."""
    wav = seed_wav(tmp_path / "x.wav")
    a = _StubByKey(backend="faster-whisper", model="small.en", text="whisper text")
    b = _StubByKey(backend="mlx-voxtral", model="voxtral-mini", text="voxtral text")
    cached_transcribe(wav, a, initial_prompt=None, hotwords=None, hallucination_rules=[_AMARA_RULE])
    cached_transcribe(wav, b, initial_prompt=None, hotwords=None, hallucination_rules=[_AMARA_RULE])
    # Operator pins A as the primary even though B was written last.
    set_primary_transcript(wav, backend="faster-whisper", model="small.en")

    cached_transcribe(wav, b, initial_prompt=None, hotwords=None, hallucination_rules=[_AMARA_RULE])

    primary = read_cached(wav)
    assert primary is not None
    assert primary.result.backend == "faster-whisper", "an unchanged re-run must not steal the primary"


# ---------------------------------------------------------------------------
# Failure paths: a partially-applied migration and a concurrently-deleted
# sidecar both used to make a transcript that IS on disk unreachable.
# ---------------------------------------------------------------------------


def _legacy_payload(wav: Path, *, backend: str, model: str, text: str) -> dict:
    st = wav.stat()
    return {
        "transcriber": "fake",
        "backend": backend,
        "device": "test-device",
        "model": model,
        "language": "en",
        "language_probability": 1.0,
        "duration": 1.0,
        "segments": [],
        "text": text,
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


def test_failed_legacy_migration_leaves_the_legacy_layout_readable(tmp_path: Path, monkeypatch):
    """If the move AND the copy fallback both fail (the Windows sharing
    violation the code's own comment names, or a read-only FS), the migration
    must leave NO empty `<wav>.transcripts/` behind.

    An empty directory is permanent data loss from the operator's point of
    view: `_resolve_sidecar_paths` takes the `is_dir()` branch, globs zero
    sidecars, and the migration never retries because it early-returns on
    `if d.exists()` — the transcript is on disk but gone from the dashboard
    forever."""
    from tapscribe.wav_cache import legacy_sidecar

    wc = sys.modules[_read_entry.__module__]
    wav = seed_wav(tmp_path / "x.wav")
    payload = _legacy_payload(wav, backend="faster-whisper", model="small.en", text="from legacy file")
    legacy_sidecar(wav).write_text(json.dumps(payload), encoding="utf-8")

    def _boom_replace(self, target):  # noqa: ARG001
        raise OSError(32, "The process cannot access the file because it is being used by another process")

    def _boom_write(path, content):  # noqa: ARG001
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "replace", _boom_replace)
    monkeypatch.setattr(wc, "atomic_write_text", _boom_write)

    # A write for a DIFFERENT (backend, model) triggers the migration. Here the
    # whole filesystem is read-only, so the triggering write raises too — the
    # point is what it leaves BEHIND.
    other = _StubByKey(backend="mlx-voxtral", model="voxtral-mini")
    with pytest.raises(OSError):
        cached_transcribe(wav, other, initial_prompt=None, hotwords=None, hallucination_rules=[])

    assert not transcripts_dir(wav).exists(), "an aborted migration must not leave an empty directory"
    assert legacy_sidecar(wav).is_file(), "the legacy sidecar must survive a failed migration"
    still_there = read_cached(wav)
    assert still_there is not None
    assert still_there.result.text == "from legacy file"

    # Once the failure clears, the next write retries the migration.
    monkeypatch.undo()
    cached_transcribe(wav, other, initial_prompt=None, hotwords=None, hallucination_rules=[])
    assert not legacy_sidecar(wav).is_file()
    assert {(c.result.backend, c.result.model) for c in read_all_cached(wav)} == {
        ("faster-whisper", "small.en"),
        ("mlx-voxtral", "voxtral-mini"),
    }


def test_read_cached_survives_a_sidecar_deleted_between_glob_and_stat(tmp_path: Path, monkeypatch):
    """`gather_sessions` walks sidecars on one worker thread while
    `delete_session_audio` rmtrees `<wav>.transcripts/` on another, so the
    newest-mtime fallback can stat a path that has just vanished. A bare
    `max(sidecars, key=p.stat)` let that FileNotFoundError escape and 500 the
    500 ms /api/state poll for the duration of a delete."""
    wav = seed_wav(tmp_path / "x.wav")
    a = _StubByKey(backend="faster-whisper", model="small.en", text="whisper text")
    b = _StubByKey(backend="mlx-voxtral", model="voxtral-mini", text="voxtral text")
    cached_transcribe(wav, a, initial_prompt=None, hotwords=None, hallucination_rules=[])
    cached_transcribe(wav, b, initial_prompt=None, hotwords=None, hallucination_rules=[])

    d = transcripts_dir(wav)
    (d / _PRIMARY_POINTER).unlink()  # force the newest-mtime fallback
    victim = d / "mlx-voxtral__voxtral-mini.json"
    survivor = d / "faster-whisper__small.en.json"
    assert victim.is_file() and survivor.is_file()

    real_stat = Path.stat

    def _racing_stat(self, **kwargs):
        if self == victim:
            raise FileNotFoundError(2, "No such file or directory", str(victim))
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", _racing_stat)

    entry = read_cached(wav)
    assert entry is not None, "a vanished sibling must not take the whole read down"
    assert entry.result.backend == "faster-whisper"
    assert read_primary_marker(wav) is not None
