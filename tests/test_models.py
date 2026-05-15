"""Tests for tapscribe.models — pure routing helpers (no actual model loads)."""

from __future__ import annotations

import json
from pathlib import Path

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


def _write_fake_ct2(tmp_path: Path, *, lang_ids=None, tokenizer_extra=None) -> Path:
    """Build a minimal ct2/ dir mimicking the on-disk layout NbAiLab ships."""
    ct2 = tmp_path / "ct2"
    ct2.mkdir()
    config: dict = {"suppress_ids": [1, 2]}
    if lang_ids is not None:
        config["lang_ids"] = lang_ids
    (ct2 / "config.json").write_text(json.dumps(config), encoding="utf-8")
    added = [
        {"id": 50257, "content": "<|endoftext|>"},
        {"id": 50258, "content": "<|startoftranscript|>"},
        {"id": 50259, "content": "<|en|>"},
        {"id": 50288, "content": "<|no|>"},
        {"id": 50289, "content": "<|nb|>"},
        {"id": 50359, "content": "<|transcribe|>"},
        {"id": 50364, "content": "<|notimestamps|>"},
        {"id": 50365, "content": "<|0.00|>"},  # timestamp — must be excluded
    ]
    if tokenizer_extra:
        added.extend(tokenizer_extra)
    (ct2 / "tokenizer.json").write_text(
        json.dumps({"added_tokens": added}), encoding="utf-8"
    )
    return ct2


def test_ensure_nb_whisper_lang_ids_injects_when_missing(tmp_path: Path):
    ct2 = _write_fake_ct2(tmp_path)  # no lang_ids set
    rewritten = models.ensure_nb_whisper_lang_ids(ct2)
    assert rewritten is True
    config = json.loads((ct2 / "config.json").read_text(encoding="utf-8"))
    assert config["lang_ids"] == sorted({50259, 50288, 50289})
    # Existing keys preserved.
    assert config["suppress_ids"] == [1, 2]


def test_ensure_nb_whisper_lang_ids_is_idempotent(tmp_path: Path):
    ct2 = _write_fake_ct2(tmp_path, lang_ids=[50259, 50288, 50289])
    assert models.ensure_nb_whisper_lang_ids(ct2) is False


def test_ensure_nb_whisper_lang_ids_skips_timestamp_and_special_tokens(tmp_path: Path):
    # Anything not matching the 2-3 letter `<|xx|>` shape must be skipped so
    # we don't accidentally label a timestamp token as a language.
    ct2 = _write_fake_ct2(tmp_path)
    models.ensure_nb_whisper_lang_ids(ct2)
    ids = json.loads((ct2 / "config.json").read_text(encoding="utf-8"))["lang_ids"]
    assert 50365 not in ids  # the `<|0.00|>` timestamp token
    assert 50257 not in ids  # endoftext
    assert 50364 not in ids  # notimestamps


def test_ensure_nb_whisper_lang_ids_missing_tokenizer_is_noop(tmp_path: Path):
    ct2 = tmp_path / "ct2"
    ct2.mkdir()
    (ct2 / "config.json").write_text(json.dumps({}), encoding="utf-8")
    assert models.ensure_nb_whisper_lang_ids(ct2) is False
