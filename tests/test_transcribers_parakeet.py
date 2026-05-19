"""Tests for ParakeetTranscriber (NeMo / CUDA-CPU path).

NeMo's `ASRModel.transcribe([paths], timestamps=True)` returns a list
whose entries carry `.text` and `.timestamp = {"word": [...],
"segment": [...]}`. We mock the model so the suite doesn't need NeMo
or the 600 MB Parakeet weights to run.

Why NeMo and not HF transformers: released `transformers` packages as
of mid-2026 don't carry the `parakeet_tdt` model type mapping; NeMo
is the official CUDA/CPU path. See `tapscribe/transcribers/parakeet.py`
docstring for context.
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


def _parakeet_response(text: str, *, segments=None, words=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        text=text,
        timestamp={"word": words or [], "segment": segments or []},
    )


def _fake_parakeet_model(response: types.SimpleNamespace) -> MagicMock:
    m = MagicMock()
    m.transcribe.return_value = [response]
    return m


def test_metadata_properties(tmp_path: Path):
    from tapscribe.transcribers.parakeet import ParakeetTranscriber

    t = ParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=_fake_parakeet_model(_parakeet_response("ok")),
        device="CUDA",
    )
    assert t.name == "parakeet"
    assert t.backend == "parakeet-nemo"
    assert t.device == "CUDA"
    assert t.model_name == "parakeet-tdt-0.6b-v3"


def test_transcribe_calls_model_with_timestamps_true(tmp_path: Path):
    """Parakeet's headline differentiator is real word/segment timestamps,
    which only emit when `timestamps=True`."""
    from tapscribe.transcribers.parakeet import ParakeetTranscriber

    fake = _fake_parakeet_model(_parakeet_response("hi"))
    t = ParakeetTranscriber(model_name="parakeet-tdt-0.6b-v3", model=fake, device="CPU")
    wav = _one_second_wav(tmp_path / "x.wav")
    t.transcribe(wav)
    kwargs = fake.transcribe.call_args.kwargs
    assert kwargs.get("timestamps") is True


def test_transcribe_attaches_words_to_segments_by_range(tmp_path: Path):
    from tapscribe.transcribers.parakeet import ParakeetTranscriber

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
    fake = _fake_parakeet_model(_parakeet_response("Hello there. How are you?", segments=seg_list, words=words))
    t = ParakeetTranscriber(model_name="parakeet-tdt-0.6b-v3", model=fake, device="CUDA")
    wav = _one_second_wav(tmp_path / "x.wav")
    r = t.transcribe(wav)
    assert isinstance(r, TranscriptionResult)
    assert r.text == "Hello there. How are you?"
    assert [s.text for s in r.segments] == ["Hello there.", "How are you?"]
    assert r.segments[0].start == 0.10
    assert r.segments[0].end == 0.80
    seg0_words = list(r.segments[0].words or ())
    assert [w.word for w in seg0_words] == ["Hello", "there"]


def test_transcribe_falls_back_to_single_segment_when_no_segment_list(tmp_path: Path):
    """Very short audio sometimes yields just `text` without segment
    timestamps; the adapter falls back to one segment covering the WAV."""
    from tapscribe.transcribers.parakeet import ParakeetTranscriber

    fake = _fake_parakeet_model(_parakeet_response("ok"))
    t = ParakeetTranscriber(model_name="parakeet-tdt-0.6b-v3", model=fake, device="CPU")
    wav = _one_second_wav(tmp_path / "x.wav")
    r = t.transcribe(wav)
    assert r.text == "ok"
    assert len(r.segments) == 1
    assert r.segments[0].start == 0.0
    assert r.segments[0].end == r.duration


def test_transcribe_records_source_language(tmp_path: Path):
    from tapscribe.transcribers.parakeet import ParakeetTranscriber

    t = ParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=_fake_parakeet_model(_parakeet_response("bonjour")),
        device="CUDA",
    )
    wav = _one_second_wav(tmp_path / "x.wav")
    r = t.transcribe(wav, source_lang="fr")
    assert r.language == "fr"
    assert r.source_language == "fr"


def test_transcribe_echoes_prompt_and_hotwords_audit_only(tmp_path: Path):
    """NeMo Parakeet has no prompt/hotwords slot. Same convention as the
    other Parakeet/Voxtral/Canary adapters: accepted, dropped, echoed
    on the result for audit parity."""
    from tapscribe.transcribers.parakeet import ParakeetTranscriber

    fake = _fake_parakeet_model(_parakeet_response("ok"))
    t = ParakeetTranscriber(model_name="parakeet-tdt-0.6b-v3", model=fake, device="CPU")
    wav = _one_second_wav(tmp_path / "x.wav")
    r = t.transcribe(wav, initial_prompt="standup notes", hotwords="Acme")
    kwargs = fake.transcribe.call_args.kwargs
    assert "initial_prompt" not in kwargs
    assert "hotwords" not in kwargs
    assert r.initial_prompt_used == "standup notes"
    assert r.hotwords_used == "Acme"


def test_load_fails_fast_without_nemo(monkeypatch):
    """If NeMo isn't installed, raise an actionable RuntimeError, not a
    deep ImportError chain. We probe the namespace package's ASR
    sub-collection because the top-level `nemo` namespace can exist
    without the ASR collection."""
    import importlib.util as importlib_util

    real_find_spec = importlib_util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name in ("nemo", "nemo.collections", "nemo.collections.asr"):
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib_util, "find_spec", fake_find_spec)

    from tapscribe.transcribers.parakeet import ParakeetTranscriber

    with pytest.raises(RuntimeError, match="NeMo"):
        ParakeetTranscriber.load("parakeet-tdt-0.6b-v3", kind="cpu")
