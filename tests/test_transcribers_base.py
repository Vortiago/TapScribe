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
    device = "test"
    model_name = "fake-model"

    def transcribe(self, path, *, initial_prompt=None, hotwords=None):  # noqa: ARG002
        return TranscriptionResult(
            transcriber="fake", device="test", model="fake-model",
            language="en", language_probability=1.0, duration=0.0,
            text="", segments=(), initial_prompt_used="", hotwords_used="",
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
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.model = "changed"  # type: ignore[misc]


def test_transcription_result_supports_dataclasses_replace_for_pipeline_steps():
    """The A2 pipeline pattern requires that downstream steps produce a new
    result via dataclasses.replace rather than mutating in place."""
    seg = TranscriptionSegment(start=0.0, end=1.0, text="hello")
    sup = TranscriptionSegment(start=2.0, end=3.0, text="thanks for watching", matched_rule="exact:thanks for watching")
    r = TranscriptionResult(
        transcriber="faster-whisper",
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
