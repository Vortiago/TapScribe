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
from typing import Any
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


# ---------------------------------------------------------------------------
# _transcribe_via_generate — the pre-decode fast path that avoids ffmpeg.
#
# These tests inject `mel_fn` so they don't need parakeet-mlx installed (it's
# Apple Silicon only and CI runs on Linux). They exercise:
#   - the happy path: mel_fn is called with PCM, model.generate(mel)[0] is
#     returned, and the wrapper transcribe() never touches model.transcribe();
#   - the fallback boundary: each per-failure-mode early return yields None
#     and the caller falls through to model.transcribe(path);
#   - the empty-list defensive return from generate (so a future upstream
#     regression doesn't IndexError into the Starlette response).
# ---------------------------------------------------------------------------


def _model_with_generate(*, sample_rate: int, generate_return: Any) -> MagicMock:
    """Build a fake model that exposes the API surface the pre-decode path
    expects: `preprocessor_config.sample_rate`, `generate(mel)`, plus the
    fallback `transcribe(path)` so a None return from the pre-decode lands
    somewhere predictable for the assertion."""
    model = MagicMock()
    model.preprocessor_config.sample_rate = sample_rate
    model.generate.return_value = generate_return
    # Sentinel result so a test that erroneously hits the fallback shows up
    # with a recognisable value instead of a MagicMock dump.
    model.transcribe.return_value = _aligned_result(
        sentences=[_aligned_sentence("fallback", 0.0, 0.1)], text="fallback"
    )
    return model


def test_pre_decode_happy_path_skips_model_transcribe(tmp_path: Path):
    """With matching sample rates and a mel_fn injected, the adapter uses
    `model.generate(mel)[0]` and never calls the ffmpeg-backed
    `model.transcribe(path)`."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    aligned = _aligned_result(sentences=[_aligned_sentence("hi", 0.0, 0.5)], text="hi")
    model = _model_with_generate(sample_rate=16000, generate_return=[aligned])
    captured: dict[str, Any] = {}

    def fake_mel(pcm, preproc):
        captured["pcm_dtype"] = pcm.dtype.name
        captured["pcm_len"] = int(pcm.shape[0])
        captured["preproc"] = preproc
        return "fake-mel-token"

    t = MlxParakeetTranscriber(model_name="parakeet-tdt-0.6b-v3", model=model, mel_fn=fake_mel)
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav)

    assert result.text == "hi"
    # mel_fn saw the recorder-format PCM, model.generate saw the resulting mel,
    # and the ffmpeg-backed model.transcribe was never invoked.
    assert captured["pcm_dtype"] == "float32"
    assert captured["pcm_len"] == 16000  # _one_second_wav() at 16 kHz
    model.generate.assert_called_once_with("fake-mel-token")
    model.transcribe.assert_not_called()


def test_pre_decode_falls_back_when_sample_rate_mismatches(tmp_path: Path):
    """If the loaded model's preprocessor expects a sample rate other than
    the recorder's 16 kHz, the pre-decode path bails out — falling back to
    `model.transcribe(path)` so parakeet-mlx's own loader can resample."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    aligned = _aligned_result(sentences=[_aligned_sentence("hi", 0.0, 0.5)], text="hi")
    model = _model_with_generate(sample_rate=8000, generate_return=[aligned])
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: pytest.fail("mel_fn should not be called"),
    )
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav)

    # Hit the fallback transcribe(path) — sentinel "fallback" text confirms it.
    assert result.text == "fallback"
    model.generate.assert_not_called()
    model.transcribe.assert_called_once()


def test_pre_decode_falls_back_when_wav_format_unexpected(tmp_path: Path):
    """A stereo / non-16kHz WAV (e.g. one a user dropped into the session
    folder manually) trips load_recorder_wav_as_pcm. The adapter routes
    that to model.transcribe(path) so ffmpeg can decode the unusual input."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    aligned = _aligned_result(sentences=[_aligned_sentence("hi", 0.0, 0.5)], text="hi")
    model = _model_with_generate(sample_rate=16000, generate_return=[aligned])
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: pytest.fail("mel_fn should not be called on rejected WAV"),
    )
    # 8 kHz instead of 16 kHz — load_recorder_wav_as_pcm raises RuntimeError.
    odd_wav = tmp_path / "odd.wav"
    with wave.open(str(odd_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(np.zeros(8000, dtype=np.int16).tobytes())

    result = t.transcribe(odd_wav)

    assert result.text == "fallback"
    model.transcribe.assert_called_once()


def test_pre_decode_falls_back_when_generate_returns_empty_list(tmp_path: Path):
    """`parakeet_mlx`'s contract is that generate() always returns at least
    one AlignedResult — but a future regression that returns [] would
    IndexError on `results[0]` mid-request. The defensive empty check routes
    through the ffmpeg fallback instead."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    model = _model_with_generate(sample_rate=16000, generate_return=[])
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: "fake-mel",
    )
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav)

    # mel_fn ran (so generate was called) but the empty list routed us to
    # the fallback transcribe(path) instead of crashing.
    model.generate.assert_called_once_with("fake-mel")
    model.transcribe.assert_called_once()
    assert result.text == "fallback"


def test_pre_decode_falls_back_when_generate_raises(tmp_path: Path):
    """An exception from mel_fn or model.generate (e.g. an mlx-internal
    shape mismatch after an upstream upgrade) routes through the fallback
    rather than tearing down the Starlette response."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    model = _model_with_generate(sample_rate=16000, generate_return=[])
    model.generate.side_effect = RuntimeError("mlx kaboom")
    t = MlxParakeetTranscriber(
        model_name="parakeet-tdt-0.6b-v3",
        model=model,
        mel_fn=lambda pcm, preproc: "fake-mel",
    )
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav)

    assert result.text == "fallback"
    model.transcribe.assert_called_once()


def test_pre_decode_falls_back_when_mel_fn_unavailable(tmp_path: Path):
    """When `mel_fn` isn't injected AND parakeet-mlx isn't importable
    (the CI case), `_resolve_mel_fn` returns None and the adapter falls
    back to the path-based transcribe."""
    from tapscribe.transcribers.mlx_parakeet import MlxParakeetTranscriber

    model = _model_with_generate(sample_rate=16000, generate_return=[])
    # No mel_fn → _resolve_mel_fn tries `import parakeet_mlx.audio` which
    # ImportErrors on CI (Linux, no parakeet-mlx installed) → None.
    t = MlxParakeetTranscriber(model_name="parakeet-tdt-0.6b-v3", model=model)
    wav = _one_second_wav(tmp_path / "x.wav")
    result = t.transcribe(wav)

    assert result.text == "fallback"
    model.generate.assert_not_called()
    model.transcribe.assert_called_once()


# ---------------------------------------------------------------------------
# Upstream API smoke test — only runs when parakeet-mlx is actually installed.
# This is the one place CI on macOS catches a rename or relocation of the
# undocumented entry points (`parakeet_mlx.audio.get_logmel`, the
# `preprocessor_config.sample_rate` attribute, the `generate(mel)` shape)
# BEFORE operators hit the regression on a host without ffmpeg. The pyproject
# upper bound (`parakeet-mlx>=0.5,<0.6`) is the primary defence; this test
# is the secondary signal.
# ---------------------------------------------------------------------------


def test_parakeet_mlx_audio_entry_points_present():
    """If parakeet-mlx is installed, the symbols mlx_parakeet imports lazily
    must exist. A future point release that renames `get_logmel` (or moves
    it out of `parakeet_mlx.audio`) trips this test before operators
    discover their batch transcribes have silently regressed to needing
    ffmpeg again."""
    pytest.importorskip("parakeet_mlx")
    from parakeet_mlx.audio import get_logmel  # noqa: F401 — import is the assertion
