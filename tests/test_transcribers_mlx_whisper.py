"""Tests for MlxWhisperTranscriber.

We don't import the real mlx-whisper (Apple Silicon only). The adapter
accepts a `transcribe_fn` callable so tests can inject a stub that
returns a canned result dict in mlx-whisper's native shape.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from tapscribe.transcribers.base import TranscriptionResult
from tapscribe.transcribers.mlx_whisper import (
    MlxWhisperTranscriber,
    mlx_whisper_repo,
)


def test_mlx_whisper_repo_known_models():
    assert mlx_whisper_repo("tiny.en") == "mlx-community/whisper-tiny.en-mlx"
    assert mlx_whisper_repo("large-v3") == "mlx-community/whisper-large-v3-mlx"


def test_mlx_whisper_repo_large_v3_turbo_has_no_mlx_suffix():
    # Upstream publishes this one without the -mlx suffix; verify we don't
    # construct the wrong repo name.
    assert mlx_whisper_repo("large-v3-turbo") == "mlx-community/whisper-large-v3-turbo"


def test_mlx_whisper_repo_falls_back_for_unknown():
    assert mlx_whisper_repo("xyz") == "mlx-community/whisper-xyz-mlx"


def _one_second_wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(16000, dtype=np.int16).tobytes())
    return path


def _stub_mlx_response(text: str = "hello world", segments=None, language="en"):
    return {
        "language": language,
        "segments": segments
        or [
            {"start": 0.0, "end": 1.0, "text": "hello world", "avg_logprob": -0.2},
        ],
        "text": text,
    }


def test_metadata_properties_reflect_constructor_args():
    t = MlxWhisperTranscriber(
        model_name="small.en",
        hf_repo="mlx-community/whisper-small.en-mlx",
        transcribe_fn=lambda *a, **kw: _stub_mlx_response(),
    )
    assert t.name == "mlx-whisper"
    assert t.model_name == "small.en"
    # Hardware-only device label; backend identifies the library separately.
    assert t.device == "Apple Silicon GPU"
    assert t.backend == "mlx-whisper"


def test_transcribe_returns_typed_result(tmp_path: Path):
    captured: dict = {}

    def stub(audio, **kwargs):
        captured.update(kwargs)
        captured["audio_type"] = type(audio).__name__
        return _stub_mlx_response()

    t = MlxWhisperTranscriber(
        model_name="small.en",
        hf_repo="mlx-community/whisper-small.en-mlx",
        transcribe_fn=stub,
    )
    result = t.transcribe(_one_second_wav(tmp_path / "x.wav"), initial_prompt="ctx")
    assert isinstance(result, TranscriptionResult)
    assert result.transcriber == "mlx-whisper"
    assert result.backend == "mlx-whisper"
    assert result.device == "Apple Silicon GPU"
    assert result.model == "small.en"
    assert len(result.segments) == 1
    assert result.segments[0].text == "hello world"
    # We pre-decoded the WAV — should be a numpy array, not a string path
    assert captured["audio_type"] == "ndarray"


def test_transcribe_folds_hotwords_into_initial_prompt(tmp_path: Path):
    """mlx-whisper has no `hotwords` kwarg, so the adapter concatenates
    hotwords into initial_prompt with a short framing line."""
    captured: dict = {}

    def stub(audio, **kwargs):
        captured.update(kwargs)
        return _stub_mlx_response()

    t = MlxWhisperTranscriber(
        model_name="small.en",
        hf_repo="mlx-community/whisper-small.en-mlx",
        transcribe_fn=stub,
    )
    t.transcribe(
        _one_second_wav(tmp_path / "x.wav"),
        initial_prompt="weekly planning",
        hotwords="Acme, Patricia",
    )
    prompt_sent = captured["initial_prompt"]
    assert "weekly planning" in prompt_sent
    assert "Acme, Patricia" in prompt_sent
    assert "Proper nouns" in prompt_sent  # framing line preserved
    assert "hotwords" not in captured  # mlx-whisper kwarg never passed


def test_transcribe_rejects_non_recorder_wav(tmp_path: Path):
    """A non-16kHz WAV is rejected at pre-decode time with an explicit
    error — no silent ffmpeg fallback. The operator gets a clear
    "convert the file" signal instead of a runtime dep they didn't
    sign up for."""
    import pytest

    def stub(*args, **kwargs):  # noqa: ARG001 — should not be called
        raise AssertionError("transcribe_fn should not be called on rejected WAV")

    t = MlxWhisperTranscriber(
        model_name="small.en",
        hf_repo="mlx-community/whisper-small.en-mlx",
        transcribe_fn=stub,
    )
    odd_wav = tmp_path / "odd.wav"
    with wave.open(str(odd_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(np.zeros(8000, dtype=np.int16).tobytes())

    with pytest.raises(RuntimeError, match="unexpected WAV format"):
        t.transcribe(odd_wav)


# ---------------------------------------------------------------------------
# Upstream API smoke test — only runs when mlx-whisper is actually installed.
# Symbol-contract check so an upstream rename or signature change of
# `mlx_whisper.transcribe` trips a test instead of crashing at request
# time on the operator's machine.
# ---------------------------------------------------------------------------


def test_mlx_whisper_upstream_contract():
    """If mlx_whisper is installed, its `transcribe` callable must exist
    and accept the `path_or_hf_repo` keyword the adapter relies on."""
    import inspect

    import pytest

    pytest.importorskip("mlx_whisper")
    import mlx_whisper  # type: ignore

    assert callable(getattr(mlx_whisper, "transcribe", None)), (
        "mlx_whisper.transcribe is the entry point the adapter calls"
    )
    sig = inspect.signature(mlx_whisper.transcribe)
    # The adapter passes `path_or_hf_repo=...` by keyword. Absence
    # means the API changed and the adapter needs updating.
    assert "path_or_hf_repo" in sig.parameters, (
        f"mlx_whisper.transcribe signature changed; expected path_or_hf_repo kwarg, "
        f"saw {sorted(sig.parameters)}"
    )
