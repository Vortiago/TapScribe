"""RED contract for #207 — backend bulk audio-retention op.

`reclaim_audio_older_than(current_session, older_than_days, *, execute)` walks the
archive and, for every session that is OLDER than the cutoff AND has a merged
transcript AND is NOT the current session, reclaims its audio via the existing
`delete_session_audio` (originals + `stripped/` go; the merged transcript + meta
STAY). `execute=False` PREVIEWS (lists the eligible sessions + sums reclaimable
bytes) without deleting anything; `execute=True` performs the reclaim.

The entire risk of this feature is the ELIGIBILITY TAXONOMY: get one edge wrong
and you either silently delete audio you must keep, or fail to reclaim what you
should. Every edge is pinned here:

  * OLD + transcribed + not-current + has-audio -> ELIGIBLE (reclaimed)
  * too-YOUNG (newer than the cutoff)           -> excluded
  * OLD but UN-transcribed                       -> excluded (NEVER lose audio that
                                                   isn't backed by a transcript)
  * the CURRENT session (even old + transcribed) -> excluded (NEVER touch live audio)
  * preview (execute=False) deletes NOTHING
  * execute deletes ONLY the eligible set and PRESERVES each reclaimed session's
    merged transcript

Out of this gate (named in the plan-spec, verified by code-review): the thin route
that exposes this op and the dashboard bulk-action UI. This file pins the
correctness-bearing core — the eligibility logic and the delete/keep guarantees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import seed_merged_transcript  # type: ignore[import-not-found]  # tests/ on sys.path
from wav_builders import seed_session  # type: ignore[import-not-found]

from tapscribe import config
from tapscribe.text import build_recorder_wav_name


def _reclaim(*args, **kwargs) -> dict:
    """Call the op via a lazy import so this module still COLLECTS before the op
    exists — the RED shows as two clean test failures, not a collection error that
    would halt the whole suite (and mask any regression during the build)."""
    from tapscribe.session_maintenance import reclaim_audio_older_than

    return reclaim_audio_older_than(*args, **kwargs)


# 2020 is older than any realistic cutoff; "young" is inside a 7-day window.
_OLD = datetime(2020, 1, 1, tzinfo=UTC)


def _wav(start: datetime) -> str:
    """A canonical recorder WAV filename whose parsed start = `start` (that start
    is what gives the session its age via gather_sessions' latest_iso)."""
    return build_recorder_wav_name(start, "alice", "a")


def _wavs(root: Path, session: str) -> list[Path]:
    return list((root / session).glob("*.wav"))


def _names(result: dict) -> set[str]:
    """The eligible/reclaimed session names, tolerant of a [str] or
    [{'session': ...}] listing shape."""
    return {s if isinstance(s, str) else s["session"] for s in result["sessions"]}


@pytest.fixture
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """Seed four sessions spanning the eligibility taxonomy. Returns
    (recordings_dir, current_session_id)."""
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path)
    young = datetime.now(UTC) - timedelta(days=1)

    # ELIGIBLE — old, has a merged transcript, not the current session.
    seed_session(tmp_path, "old-transcribed", [_wav(_OLD)])
    seed_merged_transcript(tmp_path, "old-transcribed")
    # EXCLUDED — too recent (inside the cutoff window).
    seed_session(tmp_path, "young-transcribed", [_wav(young)])
    seed_merged_transcript(tmp_path, "young-transcribed")
    # EXCLUDED — old but has NO merged transcript (audio not yet backed).
    seed_session(tmp_path, "old-untranscribed", [_wav(_OLD)])
    # EXCLUDED — old + transcribed, but it IS the live/current session.
    seed_session(tmp_path, "current", [_wav(_OLD)])
    seed_merged_transcript(tmp_path, "current")

    return tmp_path, "current"


def test_preview_lists_only_eligible_and_deletes_nothing(archive: tuple[Path, str]) -> None:
    root, current = archive
    result = _reclaim(current, older_than_days=7, execute=False)

    assert _names(result) == {"old-transcribed"}, (
        "preview must select ONLY the old + transcribed + not-current session — never the "
        "too-young, un-transcribed, or current one"
    )
    assert result["total_bytes"] > 0, "preview must report the reclaimable byte total"

    for session in ("old-transcribed", "young-transcribed", "old-untranscribed", "current"):
        assert _wavs(root, session), (
            f"a preview (execute=False) must not delete any audio (touched {session})"
        )


def test_execute_reclaims_only_eligible_and_keeps_transcripts(archive: tuple[Path, str]) -> None:
    root, current = archive
    _reclaim(current, older_than_days=7, execute=True)

    # The eligible session's audio is gone, its merged transcript survives.
    assert not _wavs(root, "old-transcribed"), "the eligible session's audio must be reclaimed"
    assert (root / "old-transcribed" / "session-transcript.json").exists(), (
        "reclaim keeps the merged transcript (audio-only delete)"
    )

    # Every excluded session's audio is untouched — these are the dangerous edges.
    assert _wavs(root, "young-transcribed"), "a too-young session's audio must be kept"
    assert _wavs(root, "old-untranscribed"), "an un-transcribed session's audio must NEVER be deleted"
    assert _wavs(root, "current"), "the CURRENT session's audio must NEVER be deleted"
