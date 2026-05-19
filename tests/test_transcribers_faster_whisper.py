"""Tests for FasterWhisperTranscriber.

We don't load the real faster-whisper model — we hand the constructor a
mock WhisperModel and verify the adapter's translation logic (kwarg
fallback, segment→TranscriptionSegment mapping, device/name metadata).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from tapscribe.transcribers.base import TranscriptionResult
from tapscribe.transcribers.faster_whisper import FasterWhisperTranscriber


@dataclass
class _FakeSegment:
    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    words: list | None = None


def _fake_model(
    segments: list[_FakeSegment],
    info_language: str = "en",
    info_lang_prob: float = 0.95,
    info_duration: float = 1.0,
):
    m = MagicMock()
    info = SimpleNamespace(
        language=info_language,
        language_probability=info_lang_prob,
        duration=info_duration,
    )
    m.transcribe.return_value = (iter(segments), info)
    return m


def test_metadata_properties_reflect_constructor_args():
    t = FasterWhisperTranscriber(model_name="small.en", model=MagicMock(), device="CPU (CTranslate2)")
    assert t.name == "faster-whisper"
    assert t.model_name == "small.en"
    assert t.device == "CPU (CTranslate2)"


def test_load_sets_hardware_only_device_and_backend_label(monkeypatch):
    """The adapter's `load()` must surface `device` as hardware-only ('CPU')
    and `backend` as the library identifier ('faster-whisper'), so the
    dashboard can render them in separate columns without parsing strings."""
    # faster_whisper isn't necessarily installed in CI; inject a stub module.
    import sys
    import types

    fake_fw = types.ModuleType("faster_whisper")

    class _FakeWhisperModel:
        def __init__(self, *a, **kw):
            pass

    fake_fw.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)

    t = FasterWhisperTranscriber.load("small.en")
    assert t.device == "CPU"
    assert t.backend == "faster-whisper"


def test_transcribe_returns_typed_result_with_segments(tmp_path: Path):
    model = _fake_model(
        [
            _FakeSegment(start=0.0, end=1.0, text="hello", avg_logprob=-0.2),
            _FakeSegment(start=1.0, end=2.0, text="world", avg_logprob=-0.3),
        ]
    )
    t = FasterWhisperTranscriber(model_name="small.en", model=model, device="CPU")

    result = t.transcribe(tmp_path / "x.wav", initial_prompt="ctx", hotwords="Acme")

    assert isinstance(result, TranscriptionResult)
    assert result.transcriber == "faster-whisper"
    assert result.backend == "faster-whisper"
    assert result.device == "CPU"
    assert result.model == "small.en"
    assert result.language == "en"
    assert len(result.segments) == 2
    assert result.segments[0].text == "hello"
    assert result.segments[0].avg_logprob == -0.2
    assert result.initial_prompt_used == "ctx"
    assert result.hotwords_used == "Acme"
    # Joined raw text
    assert result.text == "hello world"


def test_transcribe_falls_back_when_kwargs_unsupported(tmp_path: Path):
    """If the installed faster-whisper rejects optional kwargs (e.g.
    hotwords on older versions), the adapter should retry with the
    minimum-required set."""
    model = MagicMock()
    info = SimpleNamespace(language="en", language_probability=0.9, duration=1.0)
    # First call raises TypeError (unsupported kwarg); second call succeeds.
    model.transcribe.side_effect = [
        TypeError("unexpected keyword argument 'hotwords'"),
        (iter([_FakeSegment(start=0.0, end=1.0, text="ok")]), info),
    ]
    t = FasterWhisperTranscriber(model_name="small.en", model=model, device="CPU")
    result = t.transcribe(tmp_path / "x.wav", hotwords="Acme")
    assert result.segments[0].text == "ok"
    assert model.transcribe.call_count == 2
    # First call had the optional kwargs; fallback removed them
    first_call_kwargs = model.transcribe.call_args_list[0].kwargs
    second_call_kwargs = model.transcribe.call_args_list[1].kwargs
    assert "hotwords" in first_call_kwargs
    assert "hotwords" not in second_call_kwargs


def test_transcribe_handles_none_prompt_and_hotwords(tmp_path: Path):
    """Caller can pass None and the adapter still produces a clean result —
    the prompt/hotwords echo fields end up empty strings."""
    model = _fake_model([_FakeSegment(start=0.0, end=1.0, text="hello")])
    t = FasterWhisperTranscriber(model_name="small.en", model=model, device="CPU")
    result = t.transcribe(tmp_path / "x.wav")  # no prompt, no hotwords
    assert result.initial_prompt_used == ""
    assert result.hotwords_used == ""
