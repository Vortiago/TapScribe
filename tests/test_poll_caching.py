"""Tests for the /api/state poll-path caches (sessions, hallucinations,
catalog, config text).

These lock in the *invalidation* semantics — the part most likely to
regress — by asserting both the cache-hit (no recompute) and the
invalidate-on-change paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import TranscriberStub  # type: ignore[import-not-found]
from wav_builders import seed_wav  # type: ignore[import-not-found]

from tapscribe import config as _config
from tapscribe import hallucinations, runtime_probe, sessions, text
from tapscribe.wav_cache import cache_signature, cached_transcribe


@pytest.fixture(autouse=True)
def _clear_poll_caches():
    """Each test starts and ends with the module-level poll caches empty."""

    def _reset():
        sessions._WAV_DESC_CACHE.clear()
        sessions._SESSION_JSON_CACHE.clear()
        text._CONFIG_TEXT_CACHE.clear()
        hallucinations._RULES_CACHE.clear()
        runtime_probe._FIND_SPEC_CACHE.clear()

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

    w = seed_wav(tmp_path / "20260101T010000Z__alice__abc.wav")
    d1 = sessions._describe_wav(w)
    d2 = sessions._describe_wav(w)
    assert calls["n"] == 1  # second describe served from cache, no re-open
    assert d1["duration_s"] == d2["duration_s"]
    assert d1 is not d2  # fresh copy each call

    seed_wav(w, seconds=2.0)  # rewrite → size + mtime change → invalidate
    d3 = sessions._describe_wav(w)
    assert calls["n"] == 2
    assert d3["duration_s"] != d1["duration_s"]


def test_files_sig_ignores_the_growing_size_of_an_open_wav(tmp_path: Path):
    """During capture a recording WAV's on-disk size grows every ~0.5s tick.
    If that size fed files_sig, the dashboard would refetch GET /files (and the
    peaks endpoint) ~2 Hz for the whole meeting. An OPEN wav's size must not
    move the signature; the finalized size — once the tap closes and the wav
    leaves the open set — is what flips it, exactly once."""
    sd = tmp_path / "20260101T010000Z"
    sd.mkdir()
    wav = seed_wav(sd / "20260101T010000Z__alice__abc.wav")
    name = wav.name

    sig_open = sessions._describe_session(sd, jobs={}, current_session=sd.name, open_wavs={name})["files_sig"]

    # The tap keeps recording: the WAV grows on disk.
    seed_wav(wav, seconds=3.0)
    sig_open_grown = sessions._describe_session(sd, jobs={}, current_session=sd.name, open_wavs={name})[
        "files_sig"
    ]
    assert sig_open_grown == sig_open, "an open WAV's growing size must not flip files_sig"

    # Once the utterance closes (wav leaves the open set) its final size IS
    # folded in, so the dashboard refetches the listing exactly once.
    sig_closed = sessions._describe_session(sd, jobs={}, current_session=sd.name, open_wavs=set())[
        "files_sig"
    ]
    assert sig_closed != sig_open, "a closed WAV's final size must flip files_sig once"


def test_describe_wav_regions_mutation_does_not_pollute_cache(tmp_path: Path):
    w = seed_wav(tmp_path / "20260101T010000Z__alice__abc.wav")
    d1 = sessions._describe_wav(w)
    d1["regions"] = ["pollution"]  # _describe_session attaches this on the copy
    d2 = sessions._describe_wav(w)
    assert "regions" not in d2  # cached descriptor untouched by caller mutation


def test_describe_wav_invalidates_when_transcript_written(tmp_path: Path):
    w = seed_wav(tmp_path / "20260101T010000Z__alice__abc.wav")
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
    # `transcript` is now a slim marker (no body); the cache-invalidation is
    # proven by it flipping from None to a populated marker on the re-read.
    assert d2["transcript"] is not None
    assert d2["transcript"]["backend"] == "faster-whisper"
    assert "text" not in d2["transcript"]  # body fetched lazily, not in the listing
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
    w = seed_wav(tmp_path / "20260101T010000Z__alice__abc.wav")
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
# runtime_probe.is_module_available memoisation
# ---------------------------------------------------------------------------


def testis_module_available_memoises_and_clears_on_override():
    assert runtime_probe.is_module_available("os") is True
    assert runtime_probe._FIND_SPEC_CACHE.get("os") is True
    assert runtime_probe.is_module_available("definitely_not_a_real_module_xyz") is False
    assert runtime_probe._FIND_SPEC_CACHE.get("definitely_not_a_real_module_xyz") is False

    runtime_probe.set_installed_modules_for_testing(frozenset({"os"}))
    try:
        assert runtime_probe._FIND_SPEC_CACHE == {}  # setting the override clears the memo
        assert runtime_probe.is_module_available("os") is True
        assert runtime_probe.is_module_available("anything") is False
        assert runtime_probe._FIND_SPEC_CACHE == {}  # the override path never populates it
    finally:
        runtime_probe.set_installed_modules_for_testing(None)


# ---------------------------------------------------------------------------
# config-text cache (read_config: prompt / live-prompt / hotwords)
# ---------------------------------------------------------------------------


def test_config_text_cache_invalidates_on_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = tmp_path / "prompt.txt"
    f.write_text("first", encoding="utf-8")
    monkeypatch.setattr(_config, "PROMPT_FILE", f)

    assert text.read_config("prompt") == "first"
    assert text.read_config("prompt") == "first"  # served from cache
    f.write_text("second", encoding="utf-8")  # different (mtime, size)
    assert text.read_config("prompt") == "second"


# ---------------------------------------------------------------------------
# strip-meta.json cache (sessions._read_strip_meta_cached, shared _SESSION_JSON_CACHE)
# ---------------------------------------------------------------------------


def _write_strip_meta(stripped: Path, clip_names: list[str]) -> None:
    spans = [{"name": c, "start_s": 0.0, "end_s": 1.0} for c in clip_names]
    stripped.mkdir(parents=True, exist_ok=True)
    (stripped / "strip-meta.json").write_text(
        json.dumps({"files": {"a.wav": {"spans": spans}}}),
        encoding="utf-8",
    )


def test_strip_meta_cache_hits_then_invalidates_on_rewrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The poll reads strip-meta.json every tick, so it must serve from cache
    while the file is unchanged and re-parse when a re-strip rewrites it."""
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path)
    calls = {"n": 0}
    real = sessions._read_json_or_none

    def _spy(p):
        calls["n"] += 1
        return real(p)

    monkeypatch.setattr(sessions, "_read_json_or_none", _spy)

    stripped = tmp_path / "session" / "stripped"
    _write_strip_meta(stripped, ["clip1.wav"])

    m1 = sessions._read_strip_meta_cached(stripped)
    m2 = sessions._read_strip_meta_cached(stripped)
    assert calls["n"] == 1  # second read served from cache, no re-parse
    assert m1 == m2
    assert [sp["name"] for sp in m1["files"]["a.wav"]["spans"]] == ["clip1.wav"]

    _write_strip_meta(stripped, ["clip1.wav", "clip2.wav"])  # re-strip → (mtime, size) moves
    m3 = sessions._read_strip_meta_cached(stripped)
    assert calls["n"] == 2  # invalidated → re-parsed
    assert [sp["name"] for sp in m3["files"]["a.wav"]["spans"]] == ["clip1.wav", "clip2.wav"]


def test_strip_meta_cache_survives_gather_sessions_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`gather_sessions` prunes _SESSION_JSON_CACHE to the paths it walked. The
    strip-meta path must be in that keep-set (else the entry is evicted every
    tick, defeating the cache), so a second walk over an unchanged sidecar does
    NOT re-parse it."""
    monkeypatch.setattr(_config, "RECORDINGS_DIR", tmp_path)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    seed_wav(session_dir / "20260101T010000Z__alice__abc.wav")
    _write_strip_meta(session_dir / "stripped", ["clip1.wav"])

    calls = {"n": 0}
    real = sessions._read_json_or_none

    def _spy(p):
        if str(p).endswith("strip-meta.json"):
            calls["n"] += 1
        return real(p)

    monkeypatch.setattr(sessions, "_read_json_or_none", _spy)

    sessions.gather_sessions(current_session="session")
    sessions.gather_sessions(current_session="session")
    assert calls["n"] == 1  # parsed on the first walk, served from cache on the second
