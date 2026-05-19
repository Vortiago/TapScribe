"""Tests for CanaryTranscriber (NeMo / CUDA-CPU path).

NeMo's ASRModel exposes `transcribe(['path.wav'], source_lang=..., target_lang=...,
timestamps=True)` returning a list whose entries carry `.text` and
`.timestamp = {"word": [...], "segment": [...]}`. We mock the model
so the suite doesn't need NeMo installed.
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
    return types.SimpleNamespace(
        text=text,
        timestamp={"word": words or [], "segment": segments or []},
    )


def _fake_canary_model(response: types.SimpleNamespace) -> MagicMock:
    m = MagicMock()
    m.transcribe.return_value = [response]
    return m


def test_metadata_properties(tmp_path: Path):
    from tapscribe.transcribers.canary import CanaryTranscriber

    t = CanaryTranscriber(
        model_name="canary-1b-v2",
        model=_fake_canary_model(_canary_response("ok")),
        device="CUDA",
    )
    assert t.name == "canary"
    assert t.backend == "canary-nemo"
    assert t.device == "CUDA"
    assert t.model_name == "canary-1b-v2"


def test_transcribe_calls_model_with_source_and_target_lang(tmp_path: Path):
    from tapscribe.transcribers.canary import CanaryTranscriber

    fake = _fake_canary_model(_canary_response("hello"))
    t = CanaryTranscriber(model_name="canary-1b-v2", model=fake, device="CUDA")
    wav = _one_second_wav(tmp_path / "x.wav")
    t.transcribe(wav, source_lang="de", target_lang="en")
    kwargs = fake.transcribe.call_args.kwargs
    assert kwargs.get("source_lang") == "de"
    assert kwargs.get("target_lang") == "en"
    assert kwargs.get("timestamps") is True


def test_transcribe_defaults_to_english_when_no_langs(tmp_path: Path):
    from tapscribe.transcribers.canary import CanaryTranscriber

    fake = _fake_canary_model(_canary_response("hi"))
    t = CanaryTranscriber(model_name="canary-1b-v2", model=fake, device="CPU")
    wav = _one_second_wav(tmp_path / "x.wav")
    t.transcribe(wav)
    kwargs = fake.transcribe.call_args.kwargs
    assert kwargs.get("source_lang") == "en"
    assert kwargs.get("target_lang") == "en"


def test_transcribe_translation_sets_target_language(tmp_path: Path):
    from tapscribe.transcribers.canary import CanaryTranscriber

    fake = _fake_canary_model(_canary_response("hola"))
    t = CanaryTranscriber(model_name="canary-1b-v2", model=fake, device="CUDA")
    wav = _one_second_wav(tmp_path / "x.wav")
    r = t.transcribe(wav, source_lang="en", target_lang="es")
    assert isinstance(r, TranscriptionResult)
    assert r.source_language == "en"
    assert r.target_language == "es"


def test_transcribe_same_source_and_target_leaves_target_language_empty(tmp_path: Path):
    """When source == target, no translation happened; target_language
    stays empty so the dashboard doesn't show a misleading translation
    badge."""
    from tapscribe.transcribers.canary import CanaryTranscriber

    fake = _fake_canary_model(_canary_response("ok"))
    t = CanaryTranscriber(model_name="canary-1b-v2", model=fake, device="CPU")
    wav = _one_second_wav(tmp_path / "x.wav")
    r = t.transcribe(wav, source_lang="en", target_lang="en")
    assert r.source_language == "en"
    assert r.target_language == ""


def test_transcribe_attaches_words_to_segments_by_range(tmp_path: Path):
    from tapscribe.transcribers.canary import CanaryTranscriber

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
    fake = _fake_canary_model(_canary_response("...", segments=seg_list, words=words))
    t = CanaryTranscriber(model_name="canary-1b-v2", model=fake, device="CUDA")
    wav = _one_second_wav(tmp_path / "x.wav")
    r = t.transcribe(wav)
    assert [s.text for s in r.segments] == ["Hello there.", "How are you?"]
    seg0_words = list(r.segments[0].words or ())
    assert [w.word for w in seg0_words] == ["Hello", "there"]


def test_load_fails_fast_when_nemo_missing(monkeypatch):
    import importlib.util as importlib_util

    real = importlib_util.find_spec

    def fake(name, *args, **kwargs):
        # NeMo namespace package — adapters probe `nemo.collections.asr`.
        if name in ("nemo", "nemo.collections", "nemo.collections.asr"):
            return None
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib_util, "find_spec", fake)
    from tapscribe.transcribers.canary import CanaryTranscriber

    with pytest.raises(RuntimeError, match="nemo"):
        CanaryTranscriber.load("canary-1b-v2", kind="cpu")
