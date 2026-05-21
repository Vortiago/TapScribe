"""Tests for MlxCanaryTranscriber (mlx-audio 0.4.x Canary support).

mlx-audio's 0.4.x Canary API takes a loaded `Model` object and a
single `generate(audio, source_lang=…, target_lang=…, max_tokens=…)`
call that returns an `STTOutput` with a `.text` attribute. We mock
the model so the suite stays small and platform-agnostic; the live
upstream-symbol-contract test at the bottom runs only when mlx-audio
is actually installed.
"""

from __future__ import annotations

import types
import wave
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from tapscribe.transcribers.base import TranscriptionResult


def _wav_of_seconds(path: Path, seconds: float) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(int(16000 * seconds), dtype=np.int16).tobytes())
    return path


def _stt_output(text: str) -> types.SimpleNamespace:
    """Mimic mlx-audio 0.4.x `STTOutput`: only `.text` is consumed by
    the adapter (segment timings are hardcoded to 0.0 upstream and the
    adapter synthesises real timestamps from window offsets)."""
    return types.SimpleNamespace(
        text=text,
        segments=[{"text": text, "start": 0.0, "end": 0.0}],
        language="",
    )


def _fake_canary_model(responses: list[types.SimpleNamespace]) -> MagicMock:
    """Mock whose `generate(...)` returns the next queued response each
    call. The adapter calls generate once per window, so a multi-chunk
    test queues one response per chunk."""
    m = MagicMock()
    m.generate.side_effect = list(responses)
    return m


def test_metadata_properties(tmp_path: Path):
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    t = MlxCanaryTranscriber(
        model_name="canary-1b-v2",
        model=_fake_canary_model([_stt_output("ok")]),
    )
    assert t.name == "canary"
    assert t.backend == "canary-mlx"
    assert t.device == "Apple Silicon GPU"
    assert t.model_name == "canary-1b-v2"


def test_transcribe_default_source_target_english(tmp_path: Path):
    """Without explicit lang kwargs, the adapter calls generate with
    source_lang='en' and target_lang='en'."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    fake = _fake_canary_model([_stt_output("hello world")])
    t = MlxCanaryTranscriber(model_name="canary-1b-v2", model=fake)
    wav = _wav_of_seconds(tmp_path / "x.wav", 1.0)
    r = t.transcribe(wav)
    assert isinstance(r, TranscriptionResult)
    assert r.text == "hello world"
    kwargs = fake.generate.call_args.kwargs
    assert kwargs.get("source_lang") == "en"
    assert kwargs.get("target_lang") == "en"


def test_transcribe_passes_pcm_array_not_path(tmp_path: Path):
    """The adapter pre-decodes the WAV and hands the model a numpy
    float32 array — never a path string. That's what makes the path
    ffmpeg-free."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    fake = _fake_canary_model([_stt_output("ok")])
    t = MlxCanaryTranscriber(model_name="canary-1b-v2", model=fake)
    wav = _wav_of_seconds(tmp_path / "x.wav", 1.0)
    t.transcribe(wav)
    audio_arg = fake.generate.call_args.args[0]
    assert isinstance(audio_arg, np.ndarray)
    assert audio_arg.dtype.name == "float32"


def test_short_audio_calls_generate_once(tmp_path: Path):
    """A WAV shorter than one chunk goes through the loop with exactly
    one window — no stitching seam, no duplicate work."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    fake = _fake_canary_model([_stt_output("hi")])
    t = MlxCanaryTranscriber(
        model_name="canary-1b-v2",
        model=fake,
        chunk_duration_s=30.0,
        overlap_duration_s=2.0,
    )
    wav = _wav_of_seconds(tmp_path / "x.wav", 1.0)
    r = t.transcribe(wav)
    assert fake.generate.call_count == 1
    assert r.text == "hi"
    assert len(r.segments) == 1
    assert r.segments[0].start == 0.0
    assert r.segments[0].end == 1.0


def test_long_audio_is_chunked_with_window_relative_timestamps(tmp_path: Path):
    """A WAV longer than `chunk_duration_s` is split into windows and
    generate is called once per window. Segment timestamps reflect each
    window's position in the source WAV, not 0.0/0.0."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    # 70-second WAV at 30s chunks with 5s overlap → 3 windows:
    # [0, 30), [25, 55), [50, 70).
    fake = _fake_canary_model(
        [_stt_output("chunk one"), _stt_output("chunk two"), _stt_output("chunk three")]
    )
    t = MlxCanaryTranscriber(
        model_name="canary-1b-v2",
        model=fake,
        chunk_duration_s=30.0,
        overlap_duration_s=5.0,
    )
    wav = _wav_of_seconds(tmp_path / "long.wav", 70.0)
    r = t.transcribe(wav)
    assert fake.generate.call_count == 3
    assert [s.text for s in r.segments] == ["chunk one", "chunk two", "chunk three"]
    assert r.segments[0].start == 0.0
    assert r.segments[0].end == 30.0
    assert r.segments[1].start == 25.0
    assert r.segments[1].end == 55.0
    assert r.segments[2].start == 50.0
    assert r.segments[2].end == 70.0
    # Joined text glues windows verbatim — operator sees the chunked
    # output and can spot duplicates near the overlap if any.
    assert r.text == "chunk one chunk two chunk three"


def test_transcribe_translation_records_target_language(tmp_path: Path):
    """source!=target = translation; target_language flows into the result
    so the dashboard can render the translation badge."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    fake = _fake_canary_model([_stt_output("hola mundo")])
    t = MlxCanaryTranscriber(model_name="canary-1b-v2", model=fake)
    wav = _wav_of_seconds(tmp_path / "x.wav", 1.0)
    r = t.transcribe(wav, source_lang="en", target_lang="es")
    assert r.source_language == "en"
    assert r.target_language == "es"
    # `language` keeps recording the source for back-compat.
    assert r.language == "en"


def test_transcribe_passes_max_tokens_per_chunk(tmp_path: Path):
    """The adapter passes its configured max_tokens to every generate
    call — Canary 0.4.x's default of 200 truncates around 30s of speech,
    which is why we pre-chunk in the first place."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    fake = _fake_canary_model([_stt_output("ok")])
    t = MlxCanaryTranscriber(model_name="canary-1b-v2", model=fake, max_tokens_per_chunk=500)
    wav = _wav_of_seconds(tmp_path / "x.wav", 1.0)
    t.transcribe(wav)
    assert fake.generate.call_args.kwargs.get("max_tokens") == 500


def test_transcribe_echoes_prompt_and_hotwords_for_audit(tmp_path: Path):
    """Canary's mlx-audio API has no prompt/hotwords slot. Same convention
    as the other audio-LLMs: accepted, dropped, echoed on the result."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    fake = _fake_canary_model([_stt_output("ok")])
    t = MlxCanaryTranscriber(model_name="canary-1b-v2", model=fake)
    wav = _wav_of_seconds(tmp_path / "x.wav", 1.0)
    r = t.transcribe(wav, initial_prompt="standup", hotwords="Acme")
    kwargs = fake.generate.call_args.kwargs
    assert "initial_prompt" not in kwargs
    assert "hotwords" not in kwargs
    assert r.initial_prompt_used == "standup"
    assert r.hotwords_used == "Acme"


def test_transcribe_records_chunk_knobs_on_result(tmp_path: Path):
    """quality_settings should report the chunking knobs that produced
    the result so a cached sidecar shows what tuning was in effect."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    fake = _fake_canary_model([_stt_output("ok")])
    t = MlxCanaryTranscriber(
        model_name="canary-1b-v2",
        model=fake,
        chunk_duration_s=42.0,
        overlap_duration_s=3.0,
        max_tokens_per_chunk=512,
    )
    wav = _wav_of_seconds(tmp_path / "x.wav", 1.0)
    r = t.transcribe(wav)
    qs = r.quality_settings
    assert qs["chunk_duration_s"] == 42.0
    assert qs["overlap_duration_s"] == 3.0
    assert qs["max_tokens_per_chunk"] == 512
    assert qs["windows"] == 1


def test_transcribe_rejects_non_recorder_wav(tmp_path: Path):
    """A non-16kHz WAV (e.g. dropped into the session folder manually) is
    rejected at pre-decode time with an explicit error — no silent ffmpeg
    fallback. The operator gets a clear "convert the file" signal
    instead of a runtime dep they didn't sign up for."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    fake = _fake_canary_model([_stt_output("nope")])
    t = MlxCanaryTranscriber(model_name="canary-1b-v2", model=fake)
    odd_wav = tmp_path / "odd.wav"
    with wave.open(str(odd_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(np.zeros(8000, dtype=np.int16).tobytes())

    with pytest.raises(RuntimeError, match="unexpected WAV format"):
        t.transcribe(odd_wav)
    fake.generate.assert_not_called()


def test_load_fails_fast_when_mlx_audio_missing(monkeypatch):
    import importlib.util as importlib_util

    real = importlib_util.find_spec

    def fake(name, *args, **kwargs):
        if name == "mlx_audio":
            return None
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib_util, "find_spec", fake)
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    with pytest.raises(RuntimeError, match="mlx-audio"):
        MlxCanaryTranscriber.load("canary-1b-v2")


# ---------------------------------------------------------------------------
# Upstream API smoke test — only runs when mlx-audio is actually installed.
# This is the canary-equivalent of the parakeet smoke test: catches an
# upstream rename / restructure (`Canary` → `Model` → `???`) the moment
# dependabot bumps mlx-audio, before operators discover their Canary loads
# explode with ImportError on first request. The pyproject upper bound
# (`mlx-audio>=0.4,<0.5`) is the primary defence; this test is the
# secondary signal.
# ---------------------------------------------------------------------------


def test_mlx_audio_canary_upstream_contract():
    """If mlx-audio is installed, the symbols mlx_canary imports lazily
    must exist with the expected shape. A future release that renames
    `Model` or restructures the canary subpackage trips this test
    before operators hit the regression at request time."""
    pytest.importorskip("mlx_audio")
    import inspect

    from mlx_audio.stt.models.canary import Model  # type: ignore

    assert hasattr(Model, "from_pretrained"), "Model.from_pretrained classmethod is the loader"
    assert hasattr(Model, "generate"), "Model.generate is the single transcribe entry point"
    sig = inspect.signature(Model.generate)
    params = set(sig.parameters)
    # The adapter passes these by keyword; their absence means the API
    # changed shape and the adapter call needs updating.
    assert {"audio", "source_lang", "target_lang", "max_tokens"} <= params, (
        f"Model.generate signature changed; expected source_lang/target_lang/max_tokens kwargs, "
        f"saw {sorted(params)}"
    )
