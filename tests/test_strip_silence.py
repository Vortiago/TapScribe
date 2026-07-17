"""Tests for tapscribe.strip_silence — helper functions that stay on the
production path (the silero detector itself is exercised end-to-end via
the stub in conftest)."""

from __future__ import annotations

import numpy as np
import pytest

from tapscribe import strip_silence as ss
from tapscribe.strip_silence import SAMPLE_RATE, plan_strip_regions


def test_filter_low_energy_regions_drops_quiet_ones():
    sample_count = ss.SAMPLE_RATE
    samples = np.concatenate(
        [
            np.tile(np.array([8000, -8000], dtype=np.int16), sample_count // 2),  # ~-12 dBFS
            np.tile(np.array([200, -200], dtype=np.int16), sample_count // 2),  # quiet
        ]
    )
    # Build two regions matching the two halves.
    regions = [(0, sample_count), (sample_count, 2 * sample_count)]
    filtered = ss.filter_low_energy_regions(samples, regions, floor_dbfs=-40.0)
    assert filtered == [(0, sample_count)]


def test_filter_low_energy_regions_keeps_empty_when_all_below():
    samples = np.full(ss.SAMPLE_RATE, 50, dtype=np.int16)
    regions = [(0, ss.SAMPLE_RATE)]
    filtered = ss.filter_low_energy_regions(samples, regions, floor_dbfs=-20.0)
    assert filtered == []


def test_read_wav_int16_rejects_wrong_rate(tmp_path):
    import wave

    path = tmp_path / "wrong-rate.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)  # wrong: expected 16000
        w.writeframes(np.zeros(800, dtype=np.int16).tobytes())
    with pytest.raises(ValueError):
        ss.read_wav_int16(path)


@pytest.mark.real_silero
def test_detect_speech_silero_without_silero_raises_runtime_error(monkeypatch):
    """silero-vad + torch are core deps; the production path must surface
    a clear "install is corrupt" error — not silently fall back, not
    return None — when the import fails (which now signals a broken
    install rather than an opt-out).

    Opts out of the autouse silero stub via @real_silero so we exercise
    the actual import path."""
    import builtins

    real_import = builtins.__import__

    def _no_silero(name, *args, **kwargs):
        # Block both deps that the production import line uses so the test
        # outcome doesn't depend on whether torch happens to be installed.
        if name in {"torch", "silero_vad"} or name.startswith(("torch.", "silero_vad.")):
            raise ImportError(f"simulated missing dep: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_silero)
    with pytest.raises(RuntimeError, match=r"install is corrupt"):
        ss.detect_speech_silero(np.zeros(16000, dtype=np.int16), min_silence_ms=500, pad_ms=200)


def test_strip_model_is_per_thread_and_never_the_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """The strip detector's Silero model is a per-worker-thread instance:
    stable within a thread (loads amortize across strip-preview knob
    drags), distinct across threads, and NEVER an instance a live gate
    holds — silero's streaming state lives on the MODEL object, so a
    shared instance would let a strip run zero a live gate's RNN state
    mid-utterance from another thread."""
    import threading

    import tapscribe.speech_gate as sg

    loaded: list[object] = []

    class _FakeModel:
        def reset_states(self) -> None:
            pass

    def _fresh() -> object:
        m = _FakeModel()
        loaded.append(m)
        return m

    monkeypatch.setattr(sg, "load_silero_model", _fresh)
    # Fresh thread-local so an instance cached by another test can't leak in.
    monkeypatch.setattr(ss, "_SILERO_LOCAL", threading.local())

    a = ss._local_silero_model()
    b = ss._local_silero_model()
    assert a is b, "within one thread the instance must be reused (amortized load)"

    other_thread: list[object] = []
    t = threading.Thread(target=lambda: other_thread.append(ss._local_silero_model()))
    t.start()
    t.join()
    assert other_thread[0] is not a, "each thread must own its own instance"

    # And a gate built afterwards loads yet another instance — strip's
    # models and the gates' models are disjoint by construction.
    pytest.importorskip("silero_vad")
    sg.make_silero_vad(threshold=0.5, hangover_ms=400)
    gate_model = loaded[-1]
    assert gate_model is not a
    assert gate_model is not other_thread[0]


def _speech_silence_samples(bursts: int = 2, burst_s: float = 0.5, gap_s: float = 0.8) -> np.ndarray:
    """Int16 samples alternating loud square-wave bursts and zero gaps —
    the same shape the e2e builder writes, kept inline so the planner tests
    need no WAV on disk."""
    chunks = []
    n_burst = int(burst_s * SAMPLE_RATE)
    burst = np.tile(np.array([12000, -12000], dtype=np.int16), n_burst // 2 + 1)[:n_burst]
    for i in range(bursts):
        chunks.append(burst)
        if i < bursts - 1:
            chunks.append(np.zeros(int(gap_s * SAMPLE_RATE), dtype=np.int16))
    return np.concatenate(chunks)


def test_plan_strip_regions_returns_spans_stats_and_no_writes():
    samples = _speech_silence_samples(bursts=2)
    plan = plan_strip_regions(samples, min_silence_ms=400, pad_ms=50)
    assert len(plan.regions) == 2
    assert len(plan.spans) == 2
    for (s, e), span in zip(plan.regions, plan.spans, strict=True):
        assert span["start_s"] == round(s / SAMPLE_RATE, 3)
        assert span["end_s"] == round(e / SAMPLE_RATE, 3)
        assert 0.0 <= span["start_s"] < span["end_s"] <= plan.in_seconds + 0.01
    assert plan.silent is False
    assert plan.reason is None
    assert plan.detector == "silero-vad"
    assert plan.speech_seconds > 0
    assert plan.segments_filtered_below_floor == 0


def test_plan_strip_regions_knobs_change_the_plan():
    samples = _speech_silence_samples(bursts=2, gap_s=0.8)
    narrow = plan_strip_regions(samples, min_silence_ms=400, pad_ms=50)
    wide = plan_strip_regions(samples, min_silence_ms=400, pad_ms=200)
    assert len(narrow.regions) == len(wide.regions) == 2
    # A wider pad extends every interior region edge outward.
    assert wide.spans[0]["end_s"] > narrow.spans[0]["end_s"]
    assert wide.spans[1]["start_s"] < narrow.spans[1]["start_s"]
    # A min_silence longer than the gap merges the bursts into one region.
    merged = plan_strip_regions(samples, min_silence_ms=900, pad_ms=50)
    assert len(merged.regions) == 1


def test_plan_strip_regions_floor_filters_all_regions():
    samples = _speech_silence_samples(bursts=2)
    plan = plan_strip_regions(samples, min_silence_ms=400, pad_ms=50, speech_floor_db=-1.0)
    assert plan.regions == [] and plan.spans == []
    assert plan.segments_filtered_below_floor == 2
    assert plan.silent is False
    assert plan.reason is not None and "below" in plan.reason
    assert plan.detector == "silero-vad"


def test_plan_strip_regions_silence_verdicts():
    silent = plan_strip_regions(np.zeros(SAMPLE_RATE, dtype=np.int16), min_silence_ms=400, pad_ms=50)
    assert silent.silent is True
    assert silent.regions == []
    assert silent.reason is not None and "whole-file silent" in silent.reason
    assert silent.detector is None
    empty = plan_strip_regions(np.zeros(0, dtype=np.int16), min_silence_ms=400, pad_ms=50)
    assert empty.silent is True and empty.reason == "empty" and empty.in_seconds == 0.0
