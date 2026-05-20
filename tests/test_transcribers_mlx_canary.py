"""Tests for MlxCanaryTranscriber (mlx-audio Canary support).

mlx-audio's Canary path takes a loaded model object and a transcribe
call that accepts `source_lang`, `target_lang`, and `timestamps=True`.
We mock the model so the suite stays small and platform-agnostic.
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


def _canary_response(text: str, *, segments=None, words=None) -> types.SimpleNamespace:
    """Shape of one entry from `model.transcribe([...])`.

    mlx-audio's Canary returns a list (batch); each entry is an object
    with `.text` and `.timestamp = {"word": [...], "segment": [...]}`.
    """
    timestamps = {"word": words or [], "segment": segments or []}
    return types.SimpleNamespace(text=text, timestamp=timestamps)


def _fake_canary_model(response: types.SimpleNamespace) -> MagicMock:
    m = MagicMock()
    # The mlx-audio API returns a list — one entry per input file.
    m.transcribe.return_value = [response]
    return m


def test_metadata_properties(tmp_path: Path):
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    t = MlxCanaryTranscriber(
        model_name="canary-1b-v2",
        model=_fake_canary_model(_canary_response("ok")),
    )
    assert t.name == "canary"
    assert t.backend == "canary-mlx"
    assert t.device == "Apple Silicon GPU"
    assert t.model_name == "canary-1b-v2"


def test_transcribe_default_source_target_english(tmp_path: Path):
    """Without explicit lang kwargs, the adapter calls the model with
    source_lang='en' and target_lang='en' (the canonical "transcribe
    English to English" default)."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    fake = _fake_canary_model(_canary_response("hello world"))
    t = MlxCanaryTranscriber(model_name="canary-1b-v2", model=fake)
    wav = _one_second_wav(tmp_path / "x.wav")
    r = t.transcribe(wav)
    assert isinstance(r, TranscriptionResult)
    assert r.text == "hello world"
    kwargs = fake.transcribe.call_args.kwargs
    assert kwargs.get("source_lang") == "en"
    assert kwargs.get("target_lang") == "en"


def test_transcribe_uses_segment_timestamps_when_present(tmp_path: Path):
    """Canary emits both segment-level and word-level timestamps; the
    adapter prefers the segment list for `segments`, attaching words
    that fall within each segment's range."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    seg_list = [
        {"start": 0.10, "end": 0.80, "segment": "Hello there."},
        {"start": 0.90, "end": 1.50, "segment": "How are you?"},
    ]
    words = [
        {"start": 0.10, "end": 0.40, "word": "Hello"},
        {"start": 0.45, "end": 0.80, "word": "there"},
        {"start": 0.90, "end": 1.20, "word": "How"},
        {"start": 1.25, "end": 1.50, "word": "are you"},
    ]
    fake = _fake_canary_model(_canary_response("Hello there. How are you?", segments=seg_list, words=words))
    t = MlxCanaryTranscriber(model_name="canary-1b-v2", model=fake)
    wav = _one_second_wav(tmp_path / "x.wav")
    r = t.transcribe(wav)
    assert [s.text for s in r.segments] == ["Hello there.", "How are you?"]
    assert r.segments[0].start == 0.10
    assert r.segments[0].end == 0.80
    # Words inside the first segment's time range get attached.
    seg0_words = list(r.segments[0].words or ())
    assert [w.word for w in seg0_words] == ["Hello", "there"]


def test_transcribe_translation_records_target_language(tmp_path: Path):
    """source!=target = translation; target_language flows into the result
    so the dashboard can render the translation badge."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    fake = _fake_canary_model(_canary_response("hola mundo"))
    t = MlxCanaryTranscriber(model_name="canary-1b-v2", model=fake)
    wav = _one_second_wav(tmp_path / "x.wav")
    r = t.transcribe(wav, source_lang="en", target_lang="es")
    assert r.source_language == "en"
    assert r.target_language == "es"
    # `language` keeps recording the source for back-compat.
    assert r.language == "en"


def test_transcribe_requests_timestamps_true(tmp_path: Path):
    """Canary's transcribe must be called with timestamps=True to get the
    word/segment alignment; without it we'd lose word-level data."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    fake = _fake_canary_model(_canary_response("ok"))
    t = MlxCanaryTranscriber(model_name="canary-1b-v2", model=fake)
    wav = _one_second_wav(tmp_path / "x.wav")
    t.transcribe(wav)
    kwargs = fake.transcribe.call_args.kwargs
    assert kwargs.get("timestamps") is True


def test_transcribe_echoes_prompt_and_hotwords_for_audit(tmp_path: Path):
    """Canary's NeMo API has no prompt/hotwords slot. Same convention as
    the other audio-LLMs: accepted, dropped, echoed on the result."""
    from tapscribe.transcribers.mlx_canary import MlxCanaryTranscriber

    fake = _fake_canary_model(_canary_response("ok"))
    t = MlxCanaryTranscriber(model_name="canary-1b-v2", model=fake)
    wav = _one_second_wav(tmp_path / "x.wav")
    r = t.transcribe(wav, initial_prompt="standup", hotwords="Acme")
    kwargs = fake.transcribe.call_args.kwargs
    assert "initial_prompt" not in kwargs
    assert "hotwords" not in kwargs
    assert r.initial_prompt_used == "standup"
    assert r.hotwords_used == "Acme"


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
