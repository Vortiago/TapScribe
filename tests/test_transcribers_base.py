"""Tests for tapscribe.transcribers.base — the core dataclasses + Protocol."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tapscribe.transcribers.base import (
    Transcriber,
    TranscriptionResult,
    TranscriptionSegment,
    Word,
    build_transcription_result,
    default_language_for,
)


def test_default_language_for_english_only_models():
    assert default_language_for("tiny.en") == "en"
    assert default_language_for("small.en") == "en"


def test_default_language_for_nb_whisper():
    assert default_language_for("nb-whisper-medium") == "no"


def test_default_language_for_unknown_returns_none():
    assert default_language_for("large-v3") is None
    assert default_language_for("voxtral-mini") is None
    assert default_language_for("") is None


class _FakeTranscriber:
    """Used to verify the Transcriber Protocol is structural — any class
    with the right shape satisfies it, no inheritance required."""

    name = "fake"
    backend = "fake-backend"
    device = "test"
    model_name = "fake-model"

    def transcribe(self, path, *, initial_prompt=None, hotwords=None, source_lang=None, target_lang=None):  # noqa: ARG002
        return TranscriptionResult(
            transcriber="fake",
            backend="fake-backend",
            device="test",
            model="fake-model",
            language="en",
            language_probability=1.0,
            duration=0.0,
            text="",
            segments=(),
            initial_prompt_used="",
            hotwords_used="",
            quality_settings={},
        )


def test_protocol_is_structurally_satisfied_without_inheritance():
    """A class with the right shape — name/device/model_name + transcribe
    method — should satisfy the Transcriber Protocol without inheriting
    from it."""
    fake = _FakeTranscriber()
    assert isinstance(fake, Transcriber)
    # And the round-trip call shape works
    result = fake.transcribe(Path("/nonexistent.wav"))
    assert isinstance(result, TranscriptionResult)
    assert result.transcriber == "fake"


def test_word_is_a_frozen_dataclass():
    w = Word(start=0.0, end=0.5, word="hello", prob=0.9)
    assert w.word == "hello"
    with pytest.raises(dataclasses.FrozenInstanceError):
        w.word = "changed"  # type: ignore[misc]


def test_transcription_segment_defaults_avg_logprob_words_matched_rule_to_none():
    s = TranscriptionSegment(start=0.0, end=1.0, text="hello world")
    assert s.avg_logprob is None
    assert s.words is None
    assert s.matched_rule is None


def test_transcription_segment_is_frozen():
    s = TranscriptionSegment(start=0.0, end=1.0, text="hello")
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.text = "changed"  # type: ignore[misc]


def test_transcription_result_is_frozen_and_carries_segments():
    seg = TranscriptionSegment(start=0.0, end=1.0, text="hello")
    r = TranscriptionResult(
        transcriber="faster-whisper",
        backend="faster-whisper",
        device="CPU",
        model="small.en",
        language="en",
        language_probability=0.95,
        duration=1.0,
        text="hello",
        segments=(seg,),
        initial_prompt_used="",
        hotwords_used="",
        quality_settings={},
    )
    assert r.segments == (seg,)
    assert r.suppressed_hallucinations == ()  # default
    assert r.backend == "faster-whisper"
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.model = "changed"  # type: ignore[misc]


def test_transcription_result_backend_and_device_are_independent_fields():
    """`backend` (library) and `device` (hardware) are orthogonal — the
    dashboard renders them in separate columns, so the dataclass must let
    them carry distinct values."""
    seg = TranscriptionSegment(start=0.0, end=1.0, text="x")
    r = TranscriptionResult(
        transcriber="mlx-whisper",
        backend="mlx-whisper",
        device="Apple Silicon GPU",
        model="large-v3",
        language="en",
        language_probability=0.95,
        duration=1.0,
        text="x",
        segments=(seg,),
        initial_prompt_used="",
        hotwords_used="",
        quality_settings={},
    )
    # Neither field carries the other's information mixed in.
    assert r.backend == "mlx-whisper"
    assert r.device == "Apple Silicon GPU"
    assert "MLX" not in r.device  # device is hardware-only
    assert "GPU" not in r.backend  # backend is library-only


def test_transcription_result_supports_dataclasses_replace_for_pipeline_steps():
    """The A2 pipeline pattern requires that downstream steps produce a new
    result via dataclasses.replace rather than mutating in place."""
    seg = TranscriptionSegment(start=0.0, end=1.0, text="hello")
    sup = TranscriptionSegment(
        start=2.0, end=3.0, text="thanks for watching", matched_rule="exact:thanks for watching"
    )
    r = TranscriptionResult(
        transcriber="faster-whisper",
        backend="faster-whisper",
        device="CPU",
        model="small.en",
        language="en",
        language_probability=0.95,
        duration=3.0,
        text="hello thanks for watching",
        segments=(seg, sup),
        initial_prompt_used="",
        hotwords_used="",
        quality_settings={},
    )
    r2 = dataclasses.replace(r, segments=(seg,), suppressed_hallucinations=(sup,))
    assert r2.segments == (seg,)
    assert r2.suppressed_hallucinations == (sup,)
    # original untouched
    assert r.segments == (seg, sup)
    assert r.suppressed_hallucinations == ()


# ---------------------------------------------------------------------------
# build_transcription_result — the shared constructor that absorbs the audit-
# field boilerplate the per-adapter `transcribe()` methods used to repeat.
# ---------------------------------------------------------------------------


def test_build_transcription_result_reads_audit_fields_from_adapter():
    """The four audit fields (transcriber/backend/device/model) used to be
    re-spelled by hand in every adapter's `return TranscriptionResult(...)`
    literal. The shared constructor pulls them off the adapter so a future
    TranscriptionResult field-rename touches one site instead of nine."""
    adapter = _FakeTranscriber()
    seg = TranscriptionSegment(start=0.0, end=1.0, text="hi")
    result = build_transcription_result(
        adapter,
        text="hi",
        segments=(seg,),
        duration=1.0,
        language="en",
    )
    assert result.transcriber == "fake"
    assert result.backend == "fake-backend"
    assert result.device == "test"
    assert result.model == "fake-model"


def test_build_transcription_result_rounds_language_probability_to_three_dp():
    """faster-whisper's `info.language_probability` is a high-precision
    float; the wire format was always 3dp. Centralise the rounding so
    every backend's cached JSON looks consistent (even backends that
    pass 0.0 — round(0.0, 3) is still 0.0)."""
    adapter = _FakeTranscriber()
    result = build_transcription_result(
        adapter,
        text="",
        segments=(),
        duration=0.0,
        language="en",
        language_probability=0.9234567,
    )
    assert result.language_probability == 0.923


def test_build_transcription_result_rounds_duration_to_two_dp():
    """Adapters were each calling `round(wav_duration_s(...), 2)` at
    the call site. Centralising means a future change to the wire-
    format precision touches one place."""
    adapter = _FakeTranscriber()
    result = build_transcription_result(
        adapter,
        text="",
        segments=(),
        duration=1.23456,
        language="en",
    )
    assert result.duration == 1.23


def test_build_transcription_result_coerces_none_inputs_to_empty_string():
    """The wire format never carries None for prompt / hotwords /
    source_language — adapters historically did `initial_prompt or ""`
    each time. Centralise so the call site is just `initial_prompt=…`
    and the helper does the coercion."""
    adapter = _FakeTranscriber()
    result = build_transcription_result(
        adapter,
        text="",
        segments=(),
        duration=0.0,
        language="en",
        initial_prompt=None,
        hotwords=None,
        source_lang=None,
    )
    assert result.initial_prompt_used == ""
    assert result.hotwords_used == ""
    assert result.source_language == ""


def test_build_transcription_result_passes_through_non_empty_inputs():
    """Explicitly-supplied values reach the result unmolested."""
    adapter = _FakeTranscriber()
    result = build_transcription_result(
        adapter,
        text="",
        segments=(),
        duration=0.0,
        language="en",
        initial_prompt="please punctuate",
        hotwords="Acme",
        source_lang="en",
    )
    assert result.initial_prompt_used == "please punctuate"
    assert result.hotwords_used == "Acme"
    assert result.source_language == "en"


def test_build_transcription_result_blanks_target_when_equal_to_source():
    """Canary's contract: `target_language` is non-empty ONLY when
    translation actually happened (target_lang != source_lang). When
    they match — plain transcription — the field stays empty so the
    dashboard's translation badge doesn't flash for a no-op."""
    adapter = _FakeTranscriber()
    result = build_transcription_result(
        adapter,
        text="",
        segments=(),
        duration=0.0,
        language="en",
        source_lang="en",
        target_lang="en",
    )
    assert result.target_language == ""


def test_build_transcription_result_keeps_target_when_translating():
    adapter = _FakeTranscriber()
    result = build_transcription_result(
        adapter,
        text="",
        segments=(),
        duration=0.0,
        language="en",
        source_lang="en",
        target_lang="es",
    )
    assert result.target_language == "es"


def test_build_transcription_result_blanks_target_when_none():
    """Most adapters (Whisper, Voxtral, Parakeet) don't translate.
    They call without `target_lang`; the field must end up empty so
    the wire shape is uniform."""
    adapter = _FakeTranscriber()
    result = build_transcription_result(
        adapter,
        text="",
        segments=(),
        duration=0.0,
        language="en",
        source_lang="en",
    )
    assert result.target_language == ""


def test_build_transcription_result_defaults_quality_settings_to_empty_dict():
    """None → {}. Some adapters (mlx_parakeet) historically passed
    `{}` explicitly; some passed a populated dict. The helper accepts
    None as "no extras"."""
    adapter = _FakeTranscriber()
    result = build_transcription_result(
        adapter,
        text="",
        segments=(),
        duration=0.0,
        language="en",
        quality_settings=None,
    )
    assert result.quality_settings == {}
