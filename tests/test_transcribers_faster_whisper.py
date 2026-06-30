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

import pytest
from wav_builders import seed_wav  # type: ignore[import-not-found]

from tapscribe.transcribers.base import ConstrainedLanguageDetector, TranscriptionResult
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


# ---------------------------------------------------------------------------
# Constrained language detection (ADR-0009): snap auto-detection to the
# meeting's candidate set so a multi-language meeting never drifts to a
# language the operator didn't declare.
# ---------------------------------------------------------------------------


def _detect_model(all_probs):
    """A model whose detect_language returns the given (lang, prob) list."""
    m = MagicMock()
    top = max(all_probs, key=lambda lp: lp[1])
    m.detect_language.return_value = (top[0], top[1], list(all_probs))
    return m


def test_adapter_is_a_constrained_language_detector():
    t = FasterWhisperTranscriber(model_name="large-v3", model=MagicMock(), device="CPU")
    assert isinstance(t, ConstrainedLanguageDetector)


def test_detect_constrained_snaps_to_best_in_set_not_global_argmax(tmp_path: Path):
    """The acoustically-likeliest language overall is Swedish, but it isn't in
    the candidate set — the pick must be the best WITHIN {da, no}, i.e. Danish,
    never Swedish."""
    wav = seed_wav(tmp_path / "region.wav")
    model = _detect_model([("sv", 0.70), ("da", 0.20), ("no", 0.07), ("en", 0.03)])
    t = FasterWhisperTranscriber(model_name="large-v3", model=model, device="CPU")
    assert t.detect_constrained_language(wav, ("da", "no")) == "da"


def test_detect_constrained_picks_in_set_winner(tmp_path: Path):
    wav = seed_wav(tmp_path / "region.wav")
    model = _detect_model([("no", 0.55), ("da", 0.40), ("en", 0.05)])
    t = FasterWhisperTranscriber(model_name="large-v3", model=model, device="CPU")
    assert t.detect_constrained_language(wav, ("da", "no", "en")) == "no"


def test_detect_constrained_empty_set_returns_none(tmp_path: Path):
    wav = seed_wav(tmp_path / "region.wav")
    model = _detect_model([("en", 0.9)])
    t = FasterWhisperTranscriber(model_name="large-v3", model=model, device="CPU")
    assert t.detect_constrained_language(wav, ()) is None
    # No detection pass wasted when there's nothing to constrain.
    model.detect_language.assert_not_called()


def test_detect_constrained_fixed_language_model_uses_name_hint(tmp_path: Path):
    """A Norwegian-only checkpoint (nb-*) can only emit Norwegian — answer from
    the model-name hint without a detect pass, and only when 'no' is actually a
    candidate."""
    wav = seed_wav(tmp_path / "region.wav")
    model = MagicMock()
    t = FasterWhisperTranscriber(model_name="nb-whisper-small", model=model, device="CPU")
    assert t.detect_constrained_language(wav, ("da", "no")) == "no"
    assert t.detect_constrained_language(wav, ("da", "en")) is None
    model.detect_language.assert_not_called()


def test_detect_constrained_falls_back_for_non_recorder_wav(tmp_path: Path):
    """A non-recorder WAV (e.g. 44.1kHz stereo) can't take the cheap stdlib
    pre-decode. detect_constrained must fall back to None (unconstrained
    auto-detect, which transcribe() still handles) instead of raising — a
    multi-language default must never make a hand-dropped file fail outright."""
    import wave

    odd = tmp_path / "odd.wav"
    with wave.open(str(odd), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 100)
    model = MagicMock()
    t = FasterWhisperTranscriber(model_name="large-v3", model=model, device="CPU")
    assert t.detect_constrained_language(odd, ("da", "no")) is None
    model.detect_language.assert_not_called()


def test_faster_whisper_detect_language_upstream_contract():
    """Smoke-test the upstream symbol the constrained-detect path imports
    (CLAUDE.md adapter-contract convention): `WhisperModel.detect_language`
    must exist and accept an `audio` argument, returning the (lang, prob,
    all_language_probs) triple the adapter unpacks. No-ops where faster-whisper
    isn't importable; runs in full where it is, so an upstream rename fails CI
    on the pin bump instead of in production."""
    import inspect

    fw = pytest.importorskip("faster_whisper")
    detect = fw.WhisperModel.detect_language
    params = inspect.signature(detect).parameters
    assert "audio" in params, params
