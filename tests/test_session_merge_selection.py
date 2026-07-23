"""Tests for select_session_wavs — the pure WAV selection step.

Builds a tiny session directory with handcrafted WAVs of varying
properties (size, RMS, filename timestamps) and asserts the right
filters fire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from wav_builders import seed_silent_wav, seed_wav  # type: ignore[import-not-found]

from tapscribe import session_merge
from tapscribe.session_merge import SessionSelection, select_session_wavs


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
    seed_wav(session_dir / _wav_name(base + timedelta(seconds=5)))
    seed_wav(session_dir / _wav_name(base + timedelta(seconds=0)))
    seed_wav(session_dir / _wav_name(base + timedelta(seconds=10)))

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
    audible = seed_wav(session_dir / _wav_name(base + timedelta(seconds=0)))
    silent_path = seed_silent_wav(session_dir / _wav_name(base + timedelta(seconds=5), speaker="bob"))

    selection = select_session_wavs(session_dir)
    assert [w.name for w in selection.wavs] == [audible.name]
    assert silent_path.name in selection.skipped_silent


def test_select_drops_empty_or_corrupt_wavs(tmp_path: Path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    base = datetime(2026, 5, 12, 9, 19, 55, tzinfo=UTC)
    good = seed_wav(session_dir / _wav_name(base))
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
    early = seed_wav(session_dir / _wav_name(base))
    mid = seed_wav(session_dir / _wav_name(base + timedelta(minutes=5), speaker="b"))
    late = seed_wav(session_dir / _wav_name(base + timedelta(minutes=10), speaker="c"))

    from_iso = (base + timedelta(minutes=3)).isoformat()
    to_iso = (base + timedelta(minutes=7)).isoformat()
    selection = select_session_wavs(session_dir, from_iso=from_iso, to_iso=to_iso)
    names = [w.name for w in selection.wavs]
    assert mid.name in names
    assert early.name not in names
    assert late.name not in names


def test_select_does_not_read_wavs_outside_the_requested_range(tmp_path: Path, monkeypatch):
    """The range filter must run BEFORE the size / duration / RMS gates.

    `wav_rms_dbfs` reads every frame of a WAV end-to-end; applying it to the
    whole directory first means a 3-WAV range against a 400-WAV meeting opens
    and fully reads 400 files before discarding 397.
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    base = datetime(2026, 5, 12, 9, 19, 55, tzinfo=UTC)
    early = seed_wav(session_dir / _wav_name(base))
    mid = seed_wav(session_dir / _wav_name(base + timedelta(minutes=5), speaker="b"))
    late = seed_wav(session_dir / _wav_name(base + timedelta(minutes=10), speaker="c"))

    read: list[str] = []

    def _spy(fn):
        def wrapped(path):
            read.append(Path(path).name)
            return fn(path)

        return wrapped

    monkeypatch.setattr(session_merge, "wav_rms_dbfs", _spy(session_merge.wav_rms_dbfs))
    monkeypatch.setattr(session_merge, "wav_duration_s", _spy(session_merge.wav_duration_s))

    selection = select_session_wavs(
        session_dir,
        from_iso=(base + timedelta(minutes=3)).isoformat(),
        to_iso=(base + timedelta(minutes=7)).isoformat(),
    )

    assert [w.name for w in selection.wavs] == [mid.name]
    assert set(read) == {mid.name}, (
        f"out-of-range WAVs were opened before being discarded: {sorted(set(read))}"
    )
    assert early.name not in read
    assert late.name not in read


def test_select_counts_only_in_range_wavs_as_bad_or_silent(tmp_path: Path):
    """`skipped_bad` / `skipped_silent` are persisted into
    `session-transcript.json` as `skipped_*_count`, so they must describe the
    files the caller ASKED about — an out-of-range corrupt or silent WAV is not
    a skip of this range."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    base = datetime(2026, 5, 12, 9, 19, 55, tzinfo=UTC)

    # Out of range: one corrupt, one silent. Both have parseable timestamps.
    out_bad = session_dir / _wav_name(base, speaker="outbad")
    out_bad.write_bytes(b"RIFF" + b"\x00" * 30)
    seed_silent_wav(session_dir / _wav_name(base + timedelta(minutes=1), speaker="outsilent"))

    # In range: one good, one corrupt, one silent — so the gates still fire.
    good = seed_wav(session_dir / _wav_name(base + timedelta(minutes=5), speaker="good"))
    in_bad = session_dir / _wav_name(base + timedelta(minutes=5, seconds=10), speaker="inbad")
    in_bad.write_bytes(b"RIFF" + b"\x00" * 30)
    in_silent = seed_silent_wav(
        session_dir / _wav_name(base + timedelta(minutes=5, seconds=20), speaker="insilent")
    )

    selection = select_session_wavs(
        session_dir,
        from_iso=(base + timedelta(minutes=3)).isoformat(),
        to_iso=(base + timedelta(minutes=7)).isoformat(),
    )

    assert [w.name for w in selection.wavs] == [good.name]
    assert selection.skipped_bad == (in_bad.name,)
    assert selection.skipped_silent == (in_silent.name,)


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
    seed_wav(session_dir / _wav_name(base))
    # stripped sibling at the same name
    seed_wav(stripped / _wav_name(base))

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
