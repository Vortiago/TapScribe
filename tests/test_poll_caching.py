"""Tests for the /api/state poll-path caches (sessions, hallucinations,
catalog, config text).

These lock in the *invalidation* semantics — the part most likely to
regress — by asserting both the cache-hit (no recompute) and the
invalidate-on-change paths.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path

import numpy as np
import pytest
from conftest import TranscriberStub  # type: ignore[import-not-found]

from tapscribe import config as _config
from tapscribe import hallucinations, sessions, text
from tapscribe.transcribers import catalog
from tapscribe.wav_cache import cache_signature, cached_transcribe


def _seed_wav(path: Path, *, seconds: float = 1.0, amplitude: int = 8000) -> Path:
    n = int(16000 * seconds)
    samples = np.tile(np.array([amplitude, -amplitude], dtype=np.int16), n // 2 + 1)[:n]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(samples.tobytes())
    return path


@pytest.fixture(autouse=True)
def _clear_poll_caches():
    """Each test starts and ends with the module-level poll caches empty."""

    def _reset():
        sessions._WAV_DESC_CACHE.clear()
        sessions._SESSION_JSON_CACHE.clear()
        text._CONFIG_TEXT_CACHE.clear()
        hallucinations._RULES_CACHE.clear()
        catalog._FIND_SPEC_CACHE.clear()

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# Per-WAV descriptor cache (sessions._describe_wav)
# ---------------------------------------------------------------------------


def test_describe_wav_caches_until_wav_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}
    real = sessions.wav_duration_s

    def _spy(p):
        calls["n"] += 1
        return real(p)

    monkeypatch.setattr(sessions, "wav_duration_s", _spy)

    w = _seed_wav(tmp_path / "20260101T010000Z__alice__abc.wav")
    d1 = sessions._describe_wav(w)
    d2 = sessions._describe_wav(w)
    assert calls["n"] == 1  # second describe served from cache, no re-open
    assert d1["duration_s"] == d2["duration_s"]
    assert d1 is not d2  # fresh copy each call

    _seed_wav(w, seconds=2.0)  # rewrite → size + mtime change → invalidate
    d3 = sessions._describe_wav(w)
    assert calls["n"] == 2
    assert d3["duration_s"] != d1["duration_s"]


def test_describe_wav_regions_mutation_does_not_pollute_cache(tmp_path: Path):
    w = _seed_wav(tmp_path / "20260101T010000Z__alice__abc.wav")
    d1 = sessions._describe_wav(w)
    d1["regions"] = ["pollution"]  # _describe_session attaches this on the copy
    d2 = sessions._describe_wav(w)
    assert "regions" not in d2  # cached descriptor untouched by caller mutation


def test_describe_wav_invalidates_when_transcript_written(tmp_path: Path):
    w = _seed_wav(tmp_path / "20260101T010000Z__alice__abc.wav")
    d1 = sessions._describe_wav(w)
    assert d1["transcript"] is None
    assert d1["transcripts"] == []

    cached_transcribe(
        w,
        TranscriberStub(backend="faster-whisper", model="small.en", text="hello"),
        initial_prompt=None,
        hotwords=None,
        hallucination_rules=[],
        force=True,
    )

    d2 = sessions._describe_wav(w)  # sidecar signature changed → re-read
    assert d2["transcript"] is not None
    assert d2["transcript"]["text"] == "hello"
    assert any(t["backend"] == "faster-whisper" for t in d2["transcripts"])


def test_cache_signature_changes_on_inplace_retranscribe(tmp_path: Path):
    """Re-transcribing the same (backend, model) overwrites the sidecar in
    place — the directory mtime may not move, but the `_primary` pointer's
    does, so the signature must change (else the dashboard shows stale text).

    A real re-transcribe runs a model for hundreds of ms–seconds and writes
    the sidecar afterwards, so its `_primary` write always lands on a later
    mtime than the previous one. We bump the pointer's mtime explicitly rather
    than transcribing twice back-to-back, so the test doesn't depend on the
    filesystem's mtime granularity (~15 ms on Windows, which two *instant* stub
    transcribes collide inside)."""
    w = _seed_wav(tmp_path / "20260101T010000Z__alice__abc.wav")
    stub = TranscriberStub(backend="faster-whisper", model="small.en", text="v1")
    cached_transcribe(w, stub, initial_prompt=None, hotwords=None, hallucination_rules=[], force=True)
    sig1 = cache_signature(w)

    # Stand in for the seconds a real re-transcribe takes: force the _primary
    # pointer's mtime forward so the signature is guaranteed to advance.
    primary = w.with_suffix(".transcripts") / "_primary"
    future = primary.stat().st_mtime + 10
    os.utime(primary, (future, future))

    sig2 = cache_signature(w)
    assert sig1 != sig2


# ---------------------------------------------------------------------------
# hallucinations.parse_rules cache
# ---------------------------------------------------------------------------


def test_parse_rules_caches_and_invalidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = tmp_path / "hallucinations.txt"
    f.write_text("thank you for watching\n", encoding="utf-8")
    monkeypatch.setattr(_config, "HALLUCINATIONS_FILE", f)

    r1 = hallucinations.parse_rules()
    r2 = hallucinations.parse_rules()
    assert r1 is r2  # cached: same list object, no recompile
    assert len(r1) == 1

    f.write_text("re:foo\nbar\n", encoding="utf-8")  # edit → different (mtime, size)
    r3 = hallucinations.parse_rules()
    assert r3 is not r1
    assert len(r3) == 2


# ---------------------------------------------------------------------------
# catalog._is_module_available memoisation
# ---------------------------------------------------------------------------


def test_is_module_available_memoises_and_clears_on_override():
    assert catalog._is_module_available("os") is True
    assert catalog._FIND_SPEC_CACHE.get("os") is True
    assert catalog._is_module_available("definitely_not_a_real_module_xyz") is False
    assert catalog._FIND_SPEC_CACHE.get("definitely_not_a_real_module_xyz") is False

    catalog.set_installed_modules_for_testing(frozenset({"os"}))
    try:
        assert catalog._FIND_SPEC_CACHE == {}  # setting the override clears the memo
        assert catalog._is_module_available("os") is True
        assert catalog._is_module_available("anything") is False
        assert catalog._FIND_SPEC_CACHE == {}  # the override path never populates it
    finally:
        catalog.set_installed_modules_for_testing(None)


# ---------------------------------------------------------------------------
# config-text cache (read_prompt / read_live_prompt / read_hotwords)
# ---------------------------------------------------------------------------


def test_config_text_cache_invalidates_on_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = tmp_path / "prompt.txt"
    f.write_text("first", encoding="utf-8")
    monkeypatch.setattr(_config, "PROMPT_FILE", f)

    assert text.read_prompt() == "first"
    assert text.read_prompt() == "first"  # served from cache
    f.write_text("second", encoding="utf-8")  # different (mtime, size)
    assert text.read_prompt() == "second"
