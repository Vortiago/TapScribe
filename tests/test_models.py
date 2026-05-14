"""Tests for tapscribe.models — pure routing helpers (no actual model loads)."""

from __future__ import annotations

from tapscribe import models


def test_mlx_whisper_repo_known_models():
    assert models.mlx_whisper_repo("tiny.en") == "mlx-community/whisper-tiny.en-mlx"
    assert models.mlx_whisper_repo("large-v3") == "mlx-community/whisper-large-v3-mlx"


def test_mlx_whisper_repo_large_v3_turbo_has_no_mlx_suffix():
    # Upstream publishes this one without the -mlx suffix; verify we don't
    # construct the wrong repo name.
    assert models.mlx_whisper_repo("large-v3-turbo") == "mlx-community/whisper-large-v3-turbo"


def test_mlx_whisper_repo_falls_back_for_unknown():
    assert models.mlx_whisper_repo("xyz") == "mlx-community/whisper-xyz-mlx"


def test_default_language_for_english_only_models():
    assert models.default_language_for("tiny.en") == "en"
    assert models.default_language_for("small.en") == "en"


def test_default_language_for_nb_whisper():
    assert models.default_language_for("nb-whisper-medium") == "no"


def test_default_language_for_unknown_returns_none():
    assert models.default_language_for("large-v3") is None
    assert models.default_language_for("voxtral-mini") is None
    assert models.default_language_for("") is None


def test_is_voxtral_detects_prefix():
    assert models.is_voxtral("voxtral-mini") is True
    assert models.is_voxtral("Voxtral-Small") is True
    assert models.is_voxtral("large-v3") is False


def test_voxtral_repo_returns_mini():
    assert models.voxtral_repo("voxtral-mini") == "mistralai/Voxtral-Mini-3B-2507"
