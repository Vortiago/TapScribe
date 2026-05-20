"""Tests for MlxParakeetTranscriber.

We don't load real Parakeet weights (Apple Silicon only; large download).
The adapter accepts an injected loaded-model object so tests verify the
result-shape contract — including the conversion of parakeet-mlx's
`AlignedResult` (sentences → tokens, with real timestamps) into
`TranscriptionSegment` / `Word` tuples.
"""

from __future__ import annotations

import types
import wave
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from tapscribe.transcribers.base import TranscriptionResult


def _one_second_wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(16000, dtype=np.int16).tobytes())
    return path


def _aligned_token(text: str, start: float, end: float) -> types.SimpleNamespace:
    return types.SimpleNamespace(text=text, start=start, end=end, duration=end - start)


def _aligned_sentence(
    text: str, start: float, end: float, tokens: list[types.SimpleNamespace] | None = None
) -> types.SimpleNamespace:
    return types.SimpleNamespace(text=text, start=start, end=end, duration=end - start, tokens=tokens or [])


def _aligned_result(sentences: list[types.SimpleNamespace], text: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(text=text, sentences=sentences)


def _fake_parakeet_model(aligned_result: types.SimpleNamespace) -> MagicMock:
    model = MagicMock()
    model.transcribe.return_value = aligned_result
    return model


def test_metadata_properties(tmp_path: Path):
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    model = _fake_parakeet_model(_aligned_result([], ""))
    t = MlxParakeetTranscriber(model_name="parakeet-tdt-0.6b-v3", model=model)
    assert t.name == "parakeet"
    assert t.backend == "parakeet-mlx"
    assert t.device == "Apple Silicon GPU"
    assert t.model_name == "parakeet-tdt-0.6b-v3"


def test_transcribe_returns_segments_with_real_timestamps(tmp_path: Path):
    """Parakeet emits real sentence-level start/end — no fallback
    interpolation like the Voxtral adapter needs."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    aligned = _aligned_result(
        sentences=[
            _aligned_sentence("Hello there.", start=0.10, end=0.80),
            _aligned_sentence("How are you?", start=0.90, end=1.50),
        ],
        text="Hello there. How are you?",
    )
    t = MlxParakeetTranscriber(model_name="parakeet-tdt-0.6b-v3", model=_fake_parakeet_model(aligned))
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav)

    assert isinstance(result, TranscriptionResult)
    assert result.text == "Hello there. How are you?"
    assert len(result.segments) == 2
    assert result.segments[0].start == 0.10
    assert result.segments[0].end == 0.80
    assert result.segments[0].text == "Hello there."
    assert result.segments[1].start == 0.90
    assert result.segments[1].end == 1.50


def test_transcribe_propagates_word_level_timestamps(tmp_path: Path):
    """AlignedToken on each sentence becomes Word entries with the same
    timing — Parakeet's headline feature versus Voxtral's prose blob."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    tokens = [
        _aligned_token("Hello", start=0.10, end=0.40),
        _aligned_token("there", start=0.45, end=0.80),
    ]
    aligned = _aligned_result(
        sentences=[_aligned_sentence("Hello there", start=0.10, end=0.80, tokens=tokens)],
        text="Hello there",
    )
    t = MlxParakeetTranscriber(model_name="parakeet-tdt-0.6b-v3", model=_fake_parakeet_model(aligned))
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav)
    assert result.segments[0].words is not None
    words = list(result.segments[0].words)
    assert len(words) == 2
    assert words[0].word == "Hello"
    assert words[0].start == 0.10
    assert words[0].end == 0.40
    assert words[1].word == "there"


def test_transcribe_accepts_but_drops_prompt_and_hotwords_records_them(tmp_path: Path):
    """parakeet-mlx's API has no prompt/hotwords slot — adapter accepts the
    kwargs for protocol parity, drops them at the model call, and echoes
    them on the result for audit."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    aligned = _aligned_result(sentences=[_aligned_sentence("ok", 0.0, 0.5)], text="ok")
    fake = _fake_parakeet_model(aligned)
    t = MlxParakeetTranscriber(model_name="parakeet-tdt-0.6b-v3", model=fake)
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav, initial_prompt="meeting", hotwords="Acme")
    # The transcribe call to the underlying model receives no prompt /
    # hotwords kwargs — those are no-ops at this layer.
    call_kwargs = fake.transcribe.call_args.kwargs if fake.transcribe.call_args else {}
    assert "initial_prompt" not in call_kwargs
    assert "hotwords" not in call_kwargs
    assert result.initial_prompt_used == "meeting"
    assert result.hotwords_used == "Acme"


def test_transcribe_records_language_from_source_lang_or_auto(tmp_path: Path):
    """Parakeet doesn't echo a detected language; we record the explicit
    source_lang we sent in, or `'auto'` when the caller didn't pin one."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    aligned = _aligned_result(sentences=[_aligned_sentence("ok", 0.0, 0.5)], text="ok")
    t = MlxParakeetTranscriber(model_name="parakeet-tdt-0.6b-v3", model=_fake_parakeet_model(aligned))
    wav = _one_second_wav(tmp_path / "x.wav")
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
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    aligned = _aligned_result(sentences=[], text="")
    t = MlxParakeetTranscriber(model_name="parakeet-tdt-0.6b-v3", model=_fake_parakeet_model(aligned))
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav)
    assert result.segments == ()
    assert result.text == ""
