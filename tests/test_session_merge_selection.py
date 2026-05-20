"""Tests for select_session_wavs — the pure WAV selection step.

Builds a tiny session directory with handcrafted WAVs of varying
properties (size, RMS, filename timestamps) and asserts the right
filters fire.
"""

from __future__ import annotations

import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from tapscribe.session_merge import SessionSelection, select_session_wavs

SAMPLE_RATE = 16000


def _write_wav(path: Path, *, seconds: float = 1.0, amplitude: int = 8000) -> Path:
    n = int(SAMPLE_RATE * seconds)
    samples = np.tile(np.array([amplitude, -amplitude], dtype=np.int16), n // 2 + 1)[:n]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())
    return path


def _wav_name(when: datetime, speaker: str = "alice", utt: str = "00000001") -> str:
    """Filenames follow the recorder's convention so parse_wav_start works."""
    stamp = when.strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{stamp}_{speaker}_ident01_{utt}.wav"


def test_select_returns_empty_for_empty_session(tmp_path: Path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    selection = select_session_wavs(session_dir)
    assert isinstance(selection, SessionSelection)
    assert selection.wavs == ()
    assert selection.skipped_bad == ()
    assert selection.skipped_silent == ()


def test_select_returns_all_audible_wavs_sorted_by_start_time(tmp_path: Path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    base = datetime(2026, 5, 12, 9, 19, 55, tzinfo=UTC)
    _write_wav(session_dir / _wav_name(base + timedelta(seconds=5)))
    _write_wav(session_dir / _wav_name(base + timedelta(seconds=0)))
    _write_wav(session_dir / _wav_name(base + timedelta(seconds=10)))

    selection = select_session_wavs(session_dir)
    names = [w.name for w in selection.wavs]
    # Sorted alphabetically also produces the right order since timestamps are first
    assert len(names) == 3
    assert names == sorted(names)


def test_select_drops_silent_wavs_below_floor(tmp_path: Path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    base = datetime(2026, 5, 12, 9, 19, 55, tzinfo=UTC)
    # One audible (amplitude=8000 ≈ -12 dBFS) + one silent (zeros)
    audible = _write_wav(session_dir / _wav_name(base + timedelta(seconds=0)))
    silent_path = session_dir / _wav_name(base + timedelta(seconds=5), speaker="bob")
    with wave.open(str(silent_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(np.zeros(SAMPLE_RATE, dtype=np.int16).tobytes())

    selection = select_session_wavs(session_dir)
    assert [w.name for w in selection.wavs] == [audible.name]
    assert silent_path.name in selection.skipped_silent


def test_select_drops_empty_or_corrupt_wavs(tmp_path: Path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    base = datetime(2026, 5, 12, 9, 19, 55, tzinfo=UTC)
    good = _write_wav(session_dir / _wav_name(base))
    # 30-byte garbage "WAV" — too small to read
    bad = session_dir / _wav_name(base + timedelta(seconds=5), speaker="bob")
    bad.write_bytes(b"RIFF" + b"\x00" * 30)

    selection = select_session_wavs(session_dir)
    assert [w.name for w in selection.wavs] == [good.name]
    assert bad.name in selection.skipped_bad


def test_select_filters_by_iso_time_range(tmp_path: Path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    base = datetime(2026, 5, 12, 9, 19, 55, tzinfo=UTC)
    early = _write_wav(session_dir / _wav_name(base))
    mid = _write_wav(session_dir / _wav_name(base + timedelta(minutes=5), speaker="b"))
    late = _write_wav(session_dir / _wav_name(base + timedelta(minutes=10), speaker="c"))

    from_iso = (base + timedelta(minutes=3)).isoformat()
    to_iso = (base + timedelta(minutes=7)).isoformat()
    selection = select_session_wavs(session_dir, from_iso=from_iso, to_iso=to_iso)
    names = [w.name for w in selection.wavs]
    assert mid.name in names
    assert early.name not in names
    assert late.name not in names


def test_select_rejects_garbage_iso_with_value_error(tmp_path: Path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    with pytest.raises(ValueError):
        select_session_wavs(session_dir, from_iso="not-an-iso-string")


def test_select_with_source_stripped_uses_stripped_subdir(tmp_path: Path):
    session_dir = tmp_path / "session"
    stripped = session_dir / "stripped"
    stripped.mkdir(parents=True)
    base = datetime(2026, 5, 12, 9, 19, 55, tzinfo=UTC)
    # original (in session_dir) is audible — needed for the silence gate
    _write_wav(session_dir / _wav_name(base))
    # stripped sibling at the same name
    _write_wav(stripped / _wav_name(base))

    sel_original = select_session_wavs(session_dir, source="original")
    sel_stripped = select_session_wavs(session_dir, source="stripped")

    # Original selection reads from session_dir/, stripped from session_dir/stripped/
    assert sel_original.wavs[0].parent == session_dir
    assert sel_stripped.wavs[0].parent == stripped
    # Both have the same filename
    assert sel_original.wavs[0].name == sel_stripped.wavs[0].name


def test_select_stripped_missing_returns_empty(tmp_path: Path):
    """Asking for stripped/ when it doesn't exist returns an empty
    selection rather than raising — callers can detect this via the
    empty `.wavs` and react (e.g. fall back to original)."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    selection = select_session_wavs(session_dir, source="stripped")
    assert selection.wavs == ()
