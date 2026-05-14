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
    processor.apply_chat_template.return_value = fake_inputs
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


def test_transcribe_folds_context_and_hotwords_into_instruction(tmp_path: Path):
    processor, model = _voxtral_mocks()
    t = VoxtralTranscriber(
        model_name="voxtral-mini",
        processor=processor,
        model=model,
        device="cpu",
    )
    wav = _one_second_wav(tmp_path / "x.wav")
    t.transcribe(wav, initial_prompt="weekly engineering planning meeting", hotwords="Acme, Patricia")

    conv = processor.apply_chat_template.call_args.args[0]
    text_block = next(c for c in conv[0]["content"] if c["type"] == "text")
    assert "weekly engineering planning meeting" in text_block["text"]
    assert "Acme, Patricia" in text_block["text"]
    # The anti-summarisation framing is always present
    assert "Transcribe the audio verbatim" in text_block["text"]
