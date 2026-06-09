"""Tests for the split-mode strip_one_wav orchestrator in tapscribe.sessions.

The strip-silence endpoint produces one WAV per detected speech region
(not one concatenated sibling). Each output filename encodes the
absolute wall-clock start of its region so the session merge places
segments at correct times without needing a region-map sidecar.
"""

from __future__ import annotations

import wave
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from tapscribe import strip_silence as ss
from tapscribe.batch_strip import strip_one_wav
from tapscribe.text import parse_wav_speaker_slug, parse_wav_start


def _make_speech_silence(speech_lengths_s, silence_lengths_s, amplitude=8000):
    """Mirror of the helper in test_strip_silence.py — builds a deterministic
    int16 buffer alternating speech-burst / silence chunks."""
    chunks = []
    n_speech = len(speech_lengths_s)
    n_silence = len(silence_lengths_s)
    for i in range(max(n_speech, n_silence)):
        if i < n_speech:
            n = int(speech_lengths_s[i] * ss.SAMPLE_RATE)
            block = np.tile(np.array([amplitude, -amplitude], dtype=np.int16), n // 2 + 1)[:n]
            chunks.append(block)
        if i < n_silence:
            n = int(silence_lengths_s[i] * ss.SAMPLE_RATE)
            chunks.append(np.zeros(n, dtype=np.int16))
    return np.concatenate(chunks)


def _wav_name(when: datetime, speaker: str = "alice", ident: str = "ident01", utt: str = "00000001") -> str:
    """Match the recorder's filename layout so parse_wav_start / parse_wav_speaker_slug
    work on the inputs we hand to strip_one_wav."""
    stamp = when.strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{stamp}_{speaker}_{ident}_{utt}.wav"


def _write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(ss.SAMPLE_RATE)
        w.writeframes(samples.tobytes())


def _common_kwargs() -> dict:
    # detect_speech_silero is auto-stubbed by the conftest fixture so this
    # test file doesn't pull in torch on CI.
    return dict(
        min_silence_ms=400,
        pad_ms=50,
        speech_floor_db=-40.0,
    )


# ---------------------------------------------------------------------------
# Core split behavior
# ---------------------------------------------------------------------------


def test_strip_one_wav_writes_one_wav_per_speech_region(tmp_path: Path):
    """A WAV with N detected speech regions produces N output files."""
    session_dir = tmp_path / "session"
    out_dir = session_dir / "stripped"
    session_dir.mkdir()
    out_dir.mkdir()

    start = datetime(2026, 5, 12, 9, 30, 15, tzinfo=UTC)
    # 3 speech bursts separated by 1s silences → 3 regions
    samples = _make_speech_silence([1.0, 1.0, 1.0], [1.0, 1.0])
    src = session_dir / _wav_name(start)
    _write_wav(src, samples)

    result = strip_one_wav(src, out_dir, **_common_kwargs())

    written = sorted(out_dir.glob("*.wav"))
    assert len(written) == 3, f"expected 3 region WAVs, got {[w.name for w in written]}"
    assert result["written"] is True
    assert result["segments"] == 3
    assert result["regions_written"] == [w.name for w in written]


def test_split_filename_timestamp_equals_origin_plus_region_offset(tmp_path: Path):
    """The ISO prefix of each region WAV = original_start + region_start_seconds.

    With a 1s burst, 1s silence, 1s burst, 1s silence, 1s burst layout the
    region starts (in seconds from origin) are roughly 0, 2, 4 — give or
    take pad_ms. We assert each output's parse_wav_start() lands within
    one second of those targets.
    """
    session_dir = tmp_path / "session"
    out_dir = session_dir / "stripped"
    session_dir.mkdir()
    out_dir.mkdir()

    start = datetime(2026, 5, 12, 9, 30, 15, tzinfo=UTC)
    samples = _make_speech_silence([1.0, 1.0, 1.0], [1.0, 1.0])
    src = session_dir / _wav_name(start)
    _write_wav(src, samples)

    strip_one_wav(src, out_dir, **_common_kwargs())

    written = sorted(out_dir.glob("*.wav"))
    assert len(written) == 3
    # Parsed starts should be approximately origin + 0s, +2s, +4s
    # (within ±pad_ms tolerance, rounded to filename's second resolution).
    starts = [parse_wav_start(w.name) for w in written]
    assert all(s is not None for s in starts)
    expected_offsets = [0, 2, 4]
    for w, parsed, exp in zip(written, starts, expected_offsets, strict=True):
        delta = (parsed - start).total_seconds()
        assert abs(delta - exp) <= 1, (
            f"{w.name}: expected ~+{exp}s from origin {start.isoformat()}, "
            f"got +{delta}s (parsed {parsed.isoformat()})"
        )


def test_split_filename_preserves_speaker_slug(tmp_path: Path):
    """Every region WAV inherits the speaker slug from the source filename
    so the merger attributes it to the same speaker."""
    session_dir = tmp_path / "session"
    out_dir = session_dir / "stripped"
    session_dir.mkdir()
    out_dir.mkdir()

    start = datetime(2026, 5, 12, 9, 30, 15, tzinfo=UTC)
    samples = _make_speech_silence([1.0, 1.0], [1.0])
    src = session_dir / _wav_name(start, speaker="bob")
    _write_wav(src, samples)

    strip_one_wav(src, out_dir, **_common_kwargs())

    written = sorted(out_dir.glob("*.wav"))
    assert written, "expected at least one region WAV"
    for w in written:
        assert parse_wav_speaker_slug(w.name) == "bob", f"{w.name}: lost speaker slug 'bob'"


def test_split_filenames_are_unique_when_regions_share_a_second(tmp_path: Path):
    """Two regions starting within the same wall-clock second must still
    produce distinct filenames (the trailing UUID acts as a tiebreaker)."""
    session_dir = tmp_path / "session"
    out_dir = session_dir / "stripped"
    session_dir.mkdir()
    out_dir.mkdir()

    start = datetime(2026, 5, 12, 9, 30, 15, tzinfo=UTC)
    # 0.3s burst, 0.5s silence, 0.3s burst — both regions land inside
    # the same wall-clock second from origin. The 0.5s gap clears the
    # min_silence_ms=400 threshold in the stubbed silero detector.
    samples = _make_speech_silence([0.3, 0.3], [0.5])
    src = session_dir / _wav_name(start)
    _write_wav(src, samples)

    strip_one_wav(src, out_dir, **_common_kwargs())

    written = sorted(out_dir.glob("*.wav"))
    names = [w.name for w in written]
    # The detector must actually have produced two regions for the
    # uniqueness assertion to mean anything — guard against a parameter
    # change that quietly collapses the test to one region.
    assert len(names) >= 2, f"expected at least 2 regions, got {names}"
    assert len(names) == len(set(names)), f"duplicate filenames: {names}"


def test_split_no_speech_regions_writes_nothing(tmp_path: Path):
    """A WAV whose detector returns no regions writes 0 files and reports it.

    Build something the detector accepts but the speech floor rejects:
    audible enough to fire the detector, quiet enough to fall below the
    speech_floor_db gate.
    """
    session_dir = tmp_path / "session"
    out_dir = session_dir / "stripped"
    session_dir.mkdir()
    out_dir.mkdir()

    start = datetime(2026, 5, 12, 9, 30, 15, tzinfo=UTC)
    # Pure silence (well below the whole-file silence gate)
    n = ss.SAMPLE_RATE * 2
    samples = np.zeros(n, dtype=np.int16)
    src = session_dir / _wav_name(start)
    _write_wav(src, samples)

    result = strip_one_wav(src, out_dir, **_common_kwargs())

    assert list(out_dir.glob("*.wav")) == []
    assert result["written"] is False


# ---------------------------------------------------------------------------
# Merge-side integration: split outputs round-trip through select+merge with
# correct absolute timestamps.
# ---------------------------------------------------------------------------


def test_select_session_wavs_treats_split_outputs_as_independent_utterances(tmp_path: Path):
    """After splitting one origin WAV into 3 regions, select_session_wavs
    on source='stripped' returns the 3 region WAVs, sorted by their (origin
    + offset) timestamp — i.e. independently of which original they came
    from."""
    from tapscribe.session_merge import select_session_wavs

    session_dir = tmp_path / "session"
    out_dir = session_dir / "stripped"
    session_dir.mkdir()
    out_dir.mkdir()

    start = datetime(2026, 5, 12, 9, 30, 15, tzinfo=UTC)
    samples = _make_speech_silence([1.0, 1.0, 1.0], [1.0, 1.0])
    src = session_dir / _wav_name(start)
    _write_wav(src, samples)

    strip_one_wav(src, out_dir, **_common_kwargs())

    selection = select_session_wavs(session_dir, source="stripped")
    # All three region WAVs should be selected (none silent, none bad).
    assert len(selection.wavs) == 3
    # Sorted by filename = sorted by parsed wall-clock start.
    parsed_times = [parse_wav_start(w.name) for w in selection.wavs]
    for earlier, later in zip(parsed_times, parsed_times[1:], strict=False):
        assert earlier <= later, "selection.wavs is not chronologically ordered"
    # First region should be at or after origin, last region noticeably later.
    assert (parsed_times[0] - start).total_seconds() <= 1
    assert (parsed_times[-1] - start).total_seconds() >= 3


def test_merge_after_split_places_segments_at_real_wall_clock_times(tmp_path: Path):
    """End-to-end: split a WAV into 3 regions, drop a faked sidecar on each
    region WAV (as if a Transcriber had run), then merge. The resulting
    SessionTranscript's segments should land at the regions' absolute
    times — not all bunched at the origin.
    """
    from tapscribe.session_merge import merge_session, select_session_wavs

    session_dir = tmp_path / "session"
    out_dir = session_dir / "stripped"
    session_dir.mkdir()
    out_dir.mkdir()

    start = datetime(2026, 5, 12, 9, 30, 15, tzinfo=UTC)
    samples = _make_speech_silence([1.0, 1.0, 1.0], [1.0, 1.0])
    src = session_dir / _wav_name(start)
    _write_wav(src, samples)

    strip_one_wav(src, out_dir, **_common_kwargs())

    # Write a per-region cached transcript that places a single segment
    # at start=0 of the region. After the merge, that segment's abs_start
    # should equal the region's parsed wall-clock start.
    written = sorted(out_dir.glob("*.wav"))
    for idx, w in enumerate(written, start=1):
        sidecar_dir = w.with_suffix(".transcripts")
        sidecar_dir.mkdir()
        # wav_start in the sidecar mirrors what cached_transcribe writes in
        # production: parse_wav_start(wav.name). Without this the merger
        # falls back to the file's mtime (test execution time).
        wav_start_iso = parse_wav_start(w.name).isoformat()
        (sidecar_dir / "fake__fake.json").write_text(
            _fake_sidecar_json(text=f"region {idx}", duration=1.0, wav_start_iso=wav_start_iso),
            encoding="utf-8",
        )

    selection = select_session_wavs(session_dir, source="stripped")
    merged = merge_session(selection)

    assert len(merged.segments) == 3
    assert [s.text for s in merged.segments] == ["region 1", "region 2", "region 3"]
    # The 2nd region starts ~2s after the origin (1s burst + 1s silence).
    # Allow a ±1s tolerance for second-resolution filename rounding.
    delta_to_second_region = (merged.segments[1].abs_start - start).total_seconds()
    assert 1 <= delta_to_second_region <= 3, (
        f"2nd region's abs_start should be ~2s after origin, got +{delta_to_second_region}s"
    )


def _fake_sidecar_json(*, text: str, duration: float, wav_start_iso: str | None = None) -> str:
    """Minimal sidecar matching the wire format read_cached() consumes.

    Mirrors the shape produced by wav_cache.cached_transcribe so the merge
    sees real-looking inputs without requiring a Transcriber.
    """
    import json

    payload: dict = {
        "transcriber": "fake",
        "backend": "fake",
        "device": "test",
        "model": "fake",
        "language": "en",
        "language_probability": 1.0,
        "duration": duration,
        "text": text,
        "segments": [
            {
                "start": 0.0,
                "end": duration,
                "text": text,
                "avg_logprob": -0.1,
            }
        ],
        "suppressed_hallucinations": [],
        "initial_prompt_used": "",
        "hotwords_used": "",
        "quality_settings": {},
        "source_language": "",
        "target_language": "",
        "wav_size": 0,
        "wav_mtime_ns": 0,
        "transcribed_at": "2026-05-12T09:31:00+00:00",
        "transcribe_ms": 0,
        "source": "stripped",
        "speaker_name": "alice",
    }
    if wav_start_iso:
        payload["wav_start"] = wav_start_iso
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Dashboard session description: region WAVs attach to their origin original.
# ---------------------------------------------------------------------------


def test_describe_session_attaches_regions_to_origin_wav(tmp_path: Path, monkeypatch):
    """`_describe_session` buckets stripped/*.wav by (speaker_slug, ident) so
    the dashboard can render each region as a sub-row under the original
    it was split from. Sibling originals with different idents must not
    cross-contaminate."""
    from tapscribe import config
    from tapscribe.sessions import _describe_session

    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path)
    session_dir = tmp_path / "session"
    out_dir = session_dir / "stripped"
    session_dir.mkdir()
    out_dir.mkdir()

    start_a = datetime(2026, 5, 12, 9, 30, 15, tzinfo=UTC)
    start_b = datetime(2026, 5, 12, 9, 31, 30, tzinfo=UTC)
    orig_a = session_dir / _wav_name(start_a, speaker="alice", ident="aaaa1111")
    orig_b = session_dir / _wav_name(start_b, speaker="bob", ident="bbbb2222")
    _write_wav(orig_a, _make_speech_silence([1.0, 1.0, 1.0], [1.0, 1.0]))
    _write_wav(orig_b, _make_speech_silence([1.0, 1.0], [1.0]))

    strip_one_wav(orig_a, out_dir, **_common_kwargs())
    strip_one_wav(orig_b, out_dir, **_common_kwargs())

    sess = _describe_session(session_dir, jobs={}, current_session="")

    files = {f["name"]: f for f in sess["files"]}
    a_regions = files[orig_a.name]["regions"]
    b_regions = files[orig_b.name]["regions"]
    assert len(a_regions) == 3
    assert len(b_regions) == 2
    # Region speaker slugs round-trip from the parent.
    for r in a_regions:
        assert parse_wav_speaker_slug(r["name"]) == "alice"
    for r in b_regions:
        assert parse_wav_speaker_slug(r["name"]) == "bob"
    # And each region carries the per-WAV row fields the UI consumes.
    sample = a_regions[0]
    assert {
        "name",
        "size",
        "duration_s",
        "transcript",
        "transcripts",
        "wav_start",
        "wav_end",
        "speaker_name",
    } <= sample.keys()
