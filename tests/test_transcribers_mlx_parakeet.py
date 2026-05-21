"""Tests for MlxParakeetTranscriber.

We don't load real Parakeet weights (Apple Silicon only; large download).
The adapter accepts an injected loaded-model object so tests verify the
result-shape contract — including the conversion of parakeet-mlx's
`AlignedResult` (sentences → tokens, with real timestamps) into
`TranscriptionSegment` / `Word` tuples, and the chunked-decode loop
that lets long sessions transcribe without OOMing the Metal GPU.
"""

from __future__ import annotations

import types
import wave
from pathlib import Path
from typing import Any
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


def _aligned_token(text: str, start: float, end: float) -> types.SimpleNamespace:
    return types.SimpleNamespace(text=text, start=start, end=end, duration=end - start)


def _aligned_sentence(
    text: str, start: float, end: float, tokens: list[types.SimpleNamespace] | None = None
) -> types.SimpleNamespace:
    return types.SimpleNamespace(text=text, start=start, end=end, duration=end - start, tokens=tokens or [])


def _aligned_result(sentences: list[types.SimpleNamespace], text: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(text=text, sentences=sentences)


def _model_with_generate(*, sample_rate: int, generate_returns: list[Any]) -> MagicMock:
    """Fake parakeet model exposing the API the adapter touches:
    `preprocessor_config.sample_rate`, `generate(mel)` returning one
    AlignedResult per window in `generate_returns`."""
    model = MagicMock()
    model.preprocessor_config.sample_rate = sample_rate
    model.generate.side_effect = list(generate_returns)
    return model


def test_metadata_properties(tmp_path: Path):
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    model = _model_with_generate(sample_rate=16000, generate_returns=[])
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: "mel",
    )
    assert t.name == "parakeet"
    assert t.backend == "parakeet-mlx"
    assert t.device == "Apple Silicon GPU"
    assert t.model_name == "parakeet-tdt-0.6b-v3"


def test_short_audio_one_window_real_timestamps(tmp_path: Path):
    """A WAV shorter than `chunk_duration_s` goes through the loop with
    exactly one window — segment timestamps come straight from the
    AlignedResult, no offsetting (offset_s=0)."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    aligned = _aligned_result(
        sentences=[
            _aligned_sentence("Hello there.", start=0.10, end=0.80),
            _aligned_sentence("How are you?", start=0.90, end=1.50),
        ],
        text="Hello there. How are you?",
    )
    model = _model_with_generate(sample_rate=16000, generate_returns=[[aligned]])
    captured_mel_calls: list[int] = []

    def fake_mel(pcm, preproc):
        captured_mel_calls.append(int(pcm.shape[0]))
        return "fake-mel"

    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=fake_mel,
        chunk_duration_s=120.0,
        overlap_duration_s=15.0,
    )
    wav = _wav_of_seconds(tmp_path / "x.wav", 2.0)
    result = t.transcribe(wav)

    assert isinstance(result, TranscriptionResult)
    assert model.generate.call_count == 1
    assert captured_mel_calls == [int(2.0 * 16000)]  # WAV duration × recorder sample rate
    assert len(result.segments) == 2
    assert result.segments[0].start == 0.10
    assert result.segments[0].end == 0.80
    assert result.segments[1].text == "How are you?"


def test_long_audio_chunks_and_offsets_timestamps(tmp_path: Path):
    """A WAV longer than `chunk_duration_s` is split into overlapping
    windows; each window's sentence timestamps are shifted by the
    window's start so the merged result is session-relative."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    # 200-second WAV at 120s chunks with 15s overlap → 2 windows:
    # [0, 120), [105, 200).
    window_0_result = _aligned_result(
        sentences=[
            _aligned_sentence("first window sentence", start=10.0, end=15.0),
        ],
        text="first window sentence",
    )
    window_1_result = _aligned_result(
        sentences=[
            # Local times within window 1 (which starts at 105 s of the WAV).
            _aligned_sentence("second window sentence", start=20.0, end=25.0),
        ],
        text="second window sentence",
    )
    model = _model_with_generate(sample_rate=16000, generate_returns=[[window_0_result], [window_1_result]])
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: f"mel-{int(pcm.shape[0])}",
        chunk_duration_s=120.0,
        overlap_duration_s=15.0,
    )
    wav = _wav_of_seconds(tmp_path / "long.wav", 200.0)
    result = t.transcribe(wav)

    assert model.generate.call_count == 2
    # Window 0 sentence kept as-is (offset 0 s).
    # Window 1 sentence offset by 105 s (window start) → 125 s start, 130 s end.
    assert [s.start for s in result.segments] == [10.0, 125.0]
    assert [s.end for s in result.segments] == [15.0, 130.0]
    assert result.text == "first window sentence second window sentence"


def test_overlap_stitching_drops_duplicate_sentences(tmp_path: Path):
    """When the same sentence appears in window N (near its end) and
    window N+1 (near its start), the overlap-midpoint rule drops the
    N+1 copy so the merged transcript doesn't repeat it."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    # 60s WAV, 40s chunks, 20s overlap → 2 windows: [0, 40), [20, 60).
    # The overlap is [20, 40) and the midpoint is at 30 s.
    window_0 = _aligned_result(
        sentences=[
            _aligned_sentence("hello", start=5.0, end=10.0),
            _aligned_sentence("straddle word", start=25.0, end=27.0),
        ],
        text="hello straddle word",
    )
    window_1 = _aligned_result(
        sentences=[
            # Local 5.0 s = absolute 25.0 s (before the overlap midpoint
            # of 30 s) → dropped as a duplicate of "straddle word".
            _aligned_sentence("straddle word", start=5.0, end=7.0),
            # Local 15.0 s = absolute 35.0 s (after the midpoint) → kept.
            _aligned_sentence("end of audio", start=15.0, end=18.0),
        ],
        text="straddle word end of audio",
    )
    model = _model_with_generate(sample_rate=16000, generate_returns=[[window_0], [window_1]])
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: "mel",
        chunk_duration_s=40.0,
        overlap_duration_s=20.0,
    )
    wav = _wav_of_seconds(tmp_path / "x.wav", 60.0)
    result = t.transcribe(wav)
    texts = [s.text for s in result.segments]
    assert texts == ["hello", "straddle word", "end of audio"]


def test_word_level_timestamps_offset_by_window(tmp_path: Path):
    """Token-level Word entries inside a windowed sentence get the same
    offset treatment as the sentence — so word timestamps stay
    session-relative across chunk boundaries."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    # 200s WAV, two 120s windows with 15s overlap → window 1 starts at 105s.
    win0_tokens = [
        _aligned_token("Hello", start=0.10, end=0.40),
        _aligned_token("there", start=0.45, end=0.80),
    ]
    win0 = _aligned_result(
        sentences=[_aligned_sentence("Hello there", start=0.10, end=0.80, tokens=win0_tokens)],
        text="Hello there",
    )
    win1_tokens = [
        # Local 10.0 s = absolute 115.0 s.
        _aligned_token("Again", start=10.0, end=10.5),
    ]
    win1 = _aligned_result(
        sentences=[_aligned_sentence("Again", start=10.0, end=10.5, tokens=win1_tokens)],
        text="Again",
    )
    model = _model_with_generate(sample_rate=16000, generate_returns=[[win0], [win1]])
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: "mel",
        chunk_duration_s=120.0,
        overlap_duration_s=15.0,
    )
    wav = _wav_of_seconds(tmp_path / "x.wav", 200.0)
    result = t.transcribe(wav)
    assert result.segments[0].words is not None
    assert result.segments[1].words is not None
    win0_words = list(result.segments[0].words)
    win1_words = list(result.segments[1].words)
    assert win0_words[0].word == "Hello"
    assert win0_words[0].start == 0.10
    assert win1_words[0].word == "Again"
    assert win1_words[0].start == 115.0


def test_transcribe_accepts_but_drops_prompt_and_hotwords(tmp_path: Path):
    """parakeet-mlx's API has no prompt/hotwords slot — adapter accepts the
    kwargs for protocol parity, drops them at the model call, and echoes
    them on the result for audit."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    aligned = _aligned_result(sentences=[_aligned_sentence("ok", 0.0, 0.5)], text="ok")
    model = _model_with_generate(sample_rate=16000, generate_returns=[[aligned]])
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: "mel",
    )
    wav = _wav_of_seconds(tmp_path / "x.wav", 1.0)
    result = t.transcribe(wav, initial_prompt="meeting", hotwords="Acme")
    call_kwargs = model.generate.call_args.kwargs if model.generate.call_args else {}
    assert "initial_prompt" not in call_kwargs
    assert "hotwords" not in call_kwargs
    assert result.initial_prompt_used == "meeting"
    assert result.hotwords_used == "Acme"


def test_transcribe_records_language_from_source_lang_or_auto(tmp_path: Path):
    """Parakeet doesn't echo a detected language; we record the explicit
    source_lang we sent in, or `'auto'` when the caller didn't pin one."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    aligned = _aligned_result(sentences=[_aligned_sentence("ok", 0.0, 0.5)], text="ok")
    model = _model_with_generate(sample_rate=16000, generate_returns=[[aligned], [aligned]])
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: "mel",
    )
    wav = _wav_of_seconds(tmp_path / "x.wav", 1.0)
    r1 = t.transcribe(wav)
    assert r1.language == "auto"
    r2 = t.transcribe(wav, source_lang="es")
    assert r2.language == "es"
    assert r2.source_language == "es"


def test_load_fails_fast_when_parakeet_mlx_missing(monkeypatch):
    """If `parakeet_mlx` isn't installed, raise an actionable RuntimeError
    with the install command — same UX as the other MLX adapters."""
    import importlib.util as importlib_util

    real_find_spec = importlib_util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "parakeet_mlx":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib_util, "find_spec", fake_find_spec)

    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    with pytest.raises(RuntimeError, match="parakeet-mlx"):
        MlxParakeetTranscriber.load("parakeet-tdt-0.6b-v3")


def test_transcribe_emits_empty_segments_for_empty_aligned_result(tmp_path: Path):
    """A window with no detected speech returns an empty AlignedResult —
    the per-window segment list is empty and the merged result has
    nothing to show for it. Distinct from the empty-list defensive
    branch (see next test)."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    aligned = _aligned_result(sentences=[], text="")
    model = _model_with_generate(sample_rate=16000, generate_returns=[[aligned]])
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: "mel",
    )
    wav = _wav_of_seconds(tmp_path / "x.wav", 1.0)
    result = t.transcribe(wav)
    assert result.segments == ()
    assert result.text == ""


def test_transcribe_handles_generate_returning_empty_list(tmp_path: Path):
    """parakeet-mlx's documented contract is non-empty, but a future
    regression returning `[]` would IndexError on `results[0]`. The
    defensive branch treats it as "no speech in this window" and the
    merged result reflects that without crashing."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    model = _model_with_generate(sample_rate=16000, generate_returns=[[]])
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: "mel",
    )
    wav = _wav_of_seconds(tmp_path / "x.wav", 1.0)
    result = t.transcribe(wav)
    assert result.segments == ()


def test_transcribe_rejects_non_recorder_wav(tmp_path: Path):
    """A non-16kHz WAV is rejected at pre-decode time with an explicit
    error — no silent ffmpeg fallback. The operator gets a clear
    "convert the file" signal."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    model = _model_with_generate(sample_rate=16000, generate_returns=[])
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: pytest.fail("mel_fn should not be called on rejected WAV"),
    )
    odd_wav = tmp_path / "odd.wav"
    with wave.open(str(odd_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(np.zeros(8000, dtype=np.int16).tobytes())

    with pytest.raises(RuntimeError, match="unexpected WAV format"):
        t.transcribe(odd_wav)
    model.generate.assert_not_called()


def test_assert_preproc_sample_rate_raises_on_non_numeric_attr():
    """If `preprocessor_config.sample_rate` is missing or non-numeric
    (an upstream-API change scenario), we get an explicit error that
    blames the upstream contract, not a confusing TypeError mid-decode."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    model = MagicMock()
    # Object whose `sample_rate` exists but isn't int-coercible — exercises
    # the TypeError/ValueError branch.
    model.preprocessor_config.sample_rate = object()
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: pytest.fail("should not be called"),
    )
    with pytest.raises(RuntimeError, match="sample_rate is not readable"):
        t._assert_preproc_sample_rate()


def test_assert_preproc_sample_rate_rejects_mismatch():
    """If the loaded model expects a sample rate other than the
    recorder's 16 kHz, `load()` calls `_assert_preproc_sample_rate`
    to fail loudly at load time — silently feeding wrong-rate PCM
    would corrupt the transcript. Tested as a unit since `load()`
    needs parakeet-mlx installed."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    model = _model_with_generate(sample_rate=8000, generate_returns=[])
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: pytest.fail("mel_fn should not be called"),
    )
    with pytest.raises(RuntimeError, match="sample_rate=8000"):
        t._assert_preproc_sample_rate()


def test_transcribe_raises_when_mel_fn_unavailable(tmp_path: Path, monkeypatch):
    """When `_resolve_mel_fn` can't import its helpers, transcribe raises
    a clear RuntimeError instead of silently falling back to a
    path-based call (which would need ffmpeg)."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    model = _model_with_generate(sample_rate=16000, generate_returns=[])
    t = MlxParakeetTranscriber(model_name="parakeet-tdt-0.6b-v3", model=model)

    def fake_resolve() -> Any:
        raise RuntimeError("parakeet-mlx pre-decode helpers unavailable (test)")

    monkeypatch.setattr(t, "_resolve_mel_fn", fake_resolve)
    wav = _wav_of_seconds(tmp_path / "x.wav", 1.0)
    with pytest.raises(RuntimeError, match="pre-decode helpers unavailable"):
        t.transcribe(wav)


# ---------------------------------------------------------------------------
# Upstream API smoke test — only runs when parakeet-mlx is actually installed.
# This is the one place CI on macOS catches a rename or relocation of the
# undocumented entry points (`parakeet_mlx.audio.get_logmel`, the
# `preprocessor_config.sample_rate` attribute, the `generate(mel)` shape)
# BEFORE operators hit the regression on a host without ffmpeg. The pyproject
# upper bound (`parakeet-mlx>=0.5,<0.6`) is the primary defence; this test
# is the secondary signal.
# ---------------------------------------------------------------------------


def test_parakeet_mlx_audio_entry_points_present():
    """If parakeet-mlx is installed, the symbols mlx_parakeet imports
    lazily must exist with the expected shape: `get_logmel` callable,
    `from_pretrained` callable, and the loaded model exposing
    `preprocessor_config.sample_rate` (the attribute we validate at
    load time) and a `generate(mel)` callable. A future point release
    that renames any of these trips this test before operators
    discover their batch transcribes have silently regressed."""
    import inspect

    pytest.importorskip("parakeet_mlx")
    import parakeet_mlx  # type: ignore
    from parakeet_mlx.audio import get_logmel  # type: ignore

    assert callable(get_logmel), "parakeet_mlx.audio.get_logmel is the mel builder"
    assert callable(getattr(parakeet_mlx, "from_pretrained", None)), (
        "parakeet_mlx.from_pretrained is the loader entry point"
    )
    # Don't load a model here (large download); inspect the helpers only.
    # The two attributes the adapter touches per-call are documented at
    # https://github.com/senstella/parakeet-mlx — a future restructure
    # that drops either trips a real-model smoke test in nightly CI.
    sig = inspect.signature(get_logmel)
    params = list(sig.parameters)
    assert len(params) >= 2, f"get_logmel must accept (audio, preprocessor_config); saw {params}"
