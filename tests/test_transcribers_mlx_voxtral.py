"""Tests for MlxVoxtralTranscriber.

Mirrors the HF VoxtralTranscriber tests — we don't load the real model
(Apple Silicon only). The adapter accepts injected processor + model so
tests verify the result-shape contract and the apply_transcrition_request
call wiring (note the upstream typo we preserve).
"""

from __future__ import annotations

import wave
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from tapscribe.transcribers.base import TranscriptionResult
from tapscribe.transcribers.mlx_voxtral import MlxVoxtralTranscriber


def _one_second_wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(16000, dtype=np.int16).tobytes())
    return path


def _mlx_voxtral_mocks(decoded_text: str = "hello world"):
    """Build mock processor + model matching mlx_voxtral's call shape."""
    processor = MagicMock()
    fake_inputs = MagicMock()
    fake_inputs.input_ids = MagicMock()
    fake_inputs.input_ids.shape = (1, 5)
    # apply_transcrition_request returns the inputs; no .to() on mlx
    # (no device-move step — MLX arrays already live in unified memory).
    processor.apply_transcrition_request.return_value = fake_inputs
    # mlx-voxtral exposes decode (single-result) not batch_decode.
    processor.decode.return_value = decoded_text

    model = MagicMock()
    fake_output = MagicMock()
    fake_output.__getitem__.return_value = fake_output
    model.generate.return_value = fake_output
    return processor, model


def test_metadata_properties():
    processor, model = _mlx_voxtral_mocks()
    t = MlxVoxtralTranscriber(
        model_name="voxtral-mini",
        processor=processor,
        model=model,
    )
    assert t.name == "voxtral"
    assert t.backend == "mlx-voxtral"
    assert t.device == "Apple Silicon GPU"
    assert t.model_name == "voxtral-mini"


def test_transcribe_returns_single_segment_with_full_text(tmp_path: Path):
    processor, model = _mlx_voxtral_mocks(decoded_text="this is the transcript")
    t = MlxVoxtralTranscriber(model_name="voxtral-mini", processor=processor, model=model)
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav)

    assert isinstance(result, TranscriptionResult)
    assert result.transcriber == "voxtral"
    assert result.backend == "mlx-voxtral"
    assert result.device == "Apple Silicon GPU"
    assert result.model == "voxtral-mini"
    assert result.text == "this is the transcript"
    # Single-sentence output → one segment spanning the WAV.
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.text == "this is the transcript"
    assert seg.start == 0.0
    assert seg.end > 0


def test_transcribe_splits_multi_sentence_output_into_segments(tmp_path: Path):
    """Same sentence-splitting contract as the HF voxtral adapter."""
    processor, model = _mlx_voxtral_mocks(
        decoded_text="Hello there. How are you? I am fine."
    )
    t = MlxVoxtralTranscriber(model_name="voxtral-mini", processor=processor, model=model)
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav)
    assert [s.text for s in result.segments] == [
        "Hello there.",
        "How are you?",
        "I am fine.",
    ]
    for a, b in zip(result.segments, result.segments[1:], strict=False):
        assert a.end == b.start
    assert result.segments[0].start == 0.0
    assert result.segments[-1].end == result.duration
    assert result.text == "Hello there. How are you? I am fine."


def test_transcribe_calls_apply_transcrition_request_with_typo_method_name(tmp_path: Path):
    """mlx-voxtral upstream ships the method with a typo
    (`apply_transcrition_request` — missing the 'c'). The HF transformers
    package spells it correctly (`apply_transcription_request`). Lock the
    adapter to the typo'd name so a future upstream rename to match HF
    will fail this test loudly rather than crashing at runtime."""
    processor, model = _mlx_voxtral_mocks()
    t = MlxVoxtralTranscriber(model_name="voxtral-mini", processor=processor, model=model)
    wav = _one_second_wav(tmp_path / "x.wav")
    t.transcribe(wav)
    assert processor.apply_transcrition_request.called
    kwargs = processor.apply_transcrition_request.call_args.kwargs
    assert kwargs["audio"] == str(wav)


def test_transcribe_records_language_auto_when_no_hint(tmp_path: Path):
    """Same contract as the HF voxtral adapter: when we send no hint, the
    result's language field is `"auto"`, never `"?"`."""
    processor, model = _mlx_voxtral_mocks()
    t = MlxVoxtralTranscriber(model_name="voxtral-mini", processor=processor, model=model)
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav)
    assert result.language == "auto"
    # Without a hint, the language kwarg shouldn't be forwarded.
    kwargs = processor.apply_transcrition_request.call_args.kwargs
    assert "language" not in kwargs


def test_transcribe_forwards_language_hint_for_nb_model_names(tmp_path: Path):
    """`nb-*` names route through default_language_for → 'no'. Voxtral
    doesn't speak Norwegian well, but the language plumbing must still
    work for whatever fork/variant the user picks."""
    processor, model = _mlx_voxtral_mocks()
    t = MlxVoxtralTranscriber(model_name="nb-voxtral-mini", processor=processor, model=model)
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav)
    kwargs = processor.apply_transcrition_request.call_args.kwargs
    assert kwargs.get("language") == "no"
    assert result.language == "no"


def test_transcribe_drops_prompt_and_hotwords_but_records_them(tmp_path: Path):
    processor, model = _mlx_voxtral_mocks()
    t = MlxVoxtralTranscriber(model_name="voxtral-mini", processor=processor, model=model)
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav, initial_prompt="weekly planning", hotwords="Acme, Patricia")
    kwargs = processor.apply_transcrition_request.call_args.kwargs
    assert "weekly planning" not in str(kwargs)
    assert "Acme" not in str(kwargs)
    assert result.initial_prompt_used == "weekly planning"
    assert result.hotwords_used == "Acme, Patricia"


def test_load_fails_fast_with_actionable_error_when_mlx_voxtral_missing(monkeypatch):
    """If `mlx_voxtral` isn't installed, raise a clear RuntimeError with the
    install command — same UX as the HF adapter does for mistral_common."""
    import importlib.util as importlib_util

    real_find_spec = importlib_util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "mlx_voxtral":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib_util, "find_spec", fake_find_spec)

    with pytest.raises(RuntimeError, match="mlx-voxtral"):
        MlxVoxtralTranscriber.load("voxtral-mini")
