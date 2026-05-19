"""Tests for VoxtralTranscriber.

Voxtral produces one free-form text response per WAV (it's an audio-LLM,
not a segmenting Whisper). We test the result-shape contract via a
mocked processor + model — no torch / transformers required to run.
"""

from __future__ import annotations

import wave
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

from tapscribe.transcribers.base import TranscriptionResult
from tapscribe.transcribers.voxtral import VoxtralTranscriber


def _one_second_wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(16000, dtype=np.int16).tobytes())
    return path


def _voxtral_mocks(decoded_text: str = "hello world"):
    processor = MagicMock()
    fake_inputs = MagicMock()
    fake_inputs.input_ids = MagicMock()
    fake_inputs.input_ids.shape = (1, 5)
    fake_inputs.to.return_value = fake_inputs
    processor.apply_transcription_request.return_value = fake_inputs
    processor.batch_decode.return_value = [decoded_text]

    model = MagicMock()
    # generate(...) returns a tensor we slice — mock to return any object;
    # the slice ([:, prompt_len:]) is invoked but its result is fed back
    # into processor.batch_decode which we mock above.
    fake_output = MagicMock()
    fake_output.__getitem__.return_value = fake_output
    model.generate.return_value = fake_output
    return processor, model


def test_metadata_properties_reflect_constructor_args():
    processor, model = _voxtral_mocks()
    t = VoxtralTranscriber(
        model_name="voxtral-mini",
        processor=processor,
        model=model,
        device="cpu",
    )
    assert t.name == "voxtral"
    assert t.model_name == "voxtral-mini"
    assert "CPU" in t.device or "cpu" in t.device  # human-readable form


def test_transcribe_returns_single_segment_with_full_text(tmp_path: Path):
    processor, model = _voxtral_mocks(decoded_text="this is the transcript")
    t = VoxtralTranscriber(
        model_name="voxtral-mini",
        processor=processor,
        model=model,
        device="cpu",
    )
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav)

    assert isinstance(result, TranscriptionResult)
    assert result.transcriber == "voxtral"
    assert result.model == "voxtral-mini"
    assert result.text == "this is the transcript"
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.text == "this is the transcript"
    assert seg.start == 0.0
    # Single segment covers the WAV duration
    assert seg.end > 0


def test_transcribe_uses_transcription_request_with_audio_path(tmp_path: Path):
    processor, model = _voxtral_mocks()
    t = VoxtralTranscriber(
        model_name="voxtral-mini",
        processor=processor,
        model=model,
        device="cpu",
    )
    wav = _one_second_wav(tmp_path / "x.wav")
    t.transcribe(wav)

    # Voxtral is routed through apply_transcription_request (not the
    # chat-template path) because the tokenizer's chat_template is unset
    # in current transformers releases.
    kwargs = processor.apply_transcription_request.call_args.kwargs
    assert kwargs["audio"] == str(wav)
    assert "model_id" in kwargs
    # No language hint for a plain "voxtral-*" model name (auto-detect).
    assert "language" not in kwargs


def test_transcribe_drops_prompt_and_hotwords_but_records_them(tmp_path: Path):
    processor, model = _voxtral_mocks()
    t = VoxtralTranscriber(
        model_name="voxtral-mini",
        processor=processor,
        model=model,
        device="cpu",
    )
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav, initial_prompt="weekly planning", hotwords="Acme, Patricia")

    # apply_transcription_request has no hook for prompt/hotwords, so they
    # must not appear in the call kwargs — they're recorded on the result
    # only for protocol parity with the other transcribers.
    kwargs = processor.apply_transcription_request.call_args.kwargs
    assert "weekly planning" not in str(kwargs)
    assert "Acme" not in str(kwargs)
    assert result.initial_prompt_used == "weekly planning"
    assert result.hotwords_used == "Acme, Patricia"
