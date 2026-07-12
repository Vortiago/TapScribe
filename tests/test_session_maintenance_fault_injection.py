"""Fault-injection tests for `session_maintenance.py`'s error branches (#264).

`test_domain_errors_fastapi_free.py` already pins ONE OSError branch:
`delete_session_audio`'s `shutil.rmtree(stripped)` failure raising
`SessionDeleteError` (#228's FastAPI-free migration). The remaining
exception branches the #264 audit flagged as uncovered are the BEST-EFFORT
ones, where an OSError is deliberately swallowed rather than raised:

  - `prune_empty_sessions`: one folder's `shutil.rmtree` failing must not
    abort the sweep of the OTHER empty folders, and must land the failing
    folder in the `failed` list instead of raising.
  - `absorb_session`: the target's stale merged transcript / summary
    `unlink()` failing must not abort the merge, since the WAVs are already
    moved by that point, so raising would leave the operation half-applied
    (WAVs in target, source still on disk).

Seam under test: the `session_maintenance` module functions directly (the
same public boundary the route handlers call), with the filesystem faults
injected via `monkeypatch` on `shutil.rmtree` / `Path.unlink`, no real
disk-full simulation.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from wav_builders import seed_session  # type: ignore[import-not-found]  # noqa: E402  # tests/ is on sys.path

from tapscribe import session_maintenance, session_paths


@pytest.fixture
def rec_root(recorder_under_test) -> Path:
    """`recorder_under_test` (tests/conftest.py) monkeypatches
    `config.RECORDINGS_DIR` to a tmpdir; expose the recordings root so these
    direct-call tests can seed session folders the resolvers will find."""
    return Path(recorder_under_test.recordings_dir)


# ---------------------------------------------------------------------------
# prune_empty_sessions: one folder's rmtree failure must not abort the sweep
# ---------------------------------------------------------------------------


def test_prune_partial_rmtree_failure_still_prunes_the_other_empty_session(
    rec_root: Path, monkeypatch: pytest.MonkeyPatch
):
    session_paths.create_session_dir("empty-ok")
    session_paths.create_session_dir("empty-boom")
    real_rmtree = shutil.rmtree

    def _flaky_rmtree(path, *a, **k):
        if Path(path).name == "empty-boom":
            raise OSError(28, "No space left on device")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(session_maintenance.shutil, "rmtree", _flaky_rmtree)

    result = session_maintenance.prune_empty_sessions("current-session-not-seeded")

    assert result["pruned"] == ["empty-ok"], "the non-failing empty session should still be pruned"
    assert result["count"] == 1
    assert result["failed"] == [{"session": "empty-boom", "error": "delete failed"}]
    assert not (rec_root / "empty-ok").exists()
    assert (rec_root / "empty-boom").exists(), "a failed rmtree must leave the folder in place, not half-deleted"


def test_prune_rmtree_failure_does_not_raise(rec_root: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ARG001
    session_paths.create_session_dir("only-empty-and-boom")

    def _boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(session_maintenance.shutil, "rmtree", _boom)

    # Must not raise: prune_empty_sessions is a best-effort sweep, not a
    # transactional one; one folder's IO failure is reported, not fatal.
    result = session_maintenance.prune_empty_sessions("current-session-not-seeded")
    assert result["pruned"] == []
    assert result["failed"] == [{"session": "only-empty-and-boom", "error": "delete failed"}]


# ---------------------------------------------------------------------------
# absorb_session: stale target transcript/summary unlink failures are
# best-effort and must not abort an already-in-flight merge.
# ---------------------------------------------------------------------------


def _fail_unlink_for(*names: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatch `Path.unlink` to raise OSError only for paths whose
    basename is in `names`, delegating everything else to the real method,
    so setup/teardown elsewhere in the suite (and absorb's OWN sidecar
    `shutil.move` calls, which never call `unlink`) are unaffected."""
    real_unlink = Path.unlink

    def _flaky_unlink(self: Path, *a, **k):
        if self.name in names:
            raise OSError(28, "No space left on device")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)


def test_absorb_transcript_unlink_failure_does_not_abort_merge(
    rec_root: Path, monkeypatch: pytest.MonkeyPatch
):
    target = seed_session(rec_root, "tgt-transcript-boom", ["20260101T000000Z__alice__abc.wav"])
    seed_session(rec_root, "src-transcript-boom", ["20260101T010000Z__alice__def.wav"])
    (target / session_paths.FILENAME_TRANSCRIPT_JSON).write_text('{"stale": true}')

    _fail_unlink_for(session_paths.FILENAME_TRANSCRIPT_JSON, monkeypatch=monkeypatch)

    result = session_maintenance.absorb_session("tgt-transcript-boom", "src-transcript-boom")

    # The merge itself completed despite the unlink failure: WAVs moved,
    # source gone. This is what "best-effort" buys: raising here would
    # leave the WAVs already moved but the source folder still present, a
    # worse half-applied state than a stale transcript file lingering.
    assert result["wavs_moved"] == 1
    assert not (rec_root / "src-transcript-boom").exists()
    # `transcript_invalidated` reflects "this transcript IS now stale", set
    # from `.exists()` BEFORE the unlink attempt: it stays True even though
    # the file itself could not be removed (documented current semantics,
    # not asserted as a bug: the operator's next "transcribe whole session"
    # overwrites the file regardless of whether it lingers meanwhile).
    assert result["transcript_invalidated"] is True
    assert (target / session_paths.FILENAME_TRANSCRIPT_JSON).exists(), (
        "the unlink() genuinely failed, so the stale file must still be on disk"
    )


def test_absorb_summary_unlink_failure_does_not_abort_merge(rec_root: Path, monkeypatch: pytest.MonkeyPatch):
    target = seed_session(rec_root, "tgt-summary-boom", ["20260101T000000Z__alice__abc.wav"])
    seed_session(rec_root, "src-summary-boom", ["20260101T010000Z__alice__def.wav"])
    (target / session_paths.FILENAME_SUMMARY_JSON).write_text('{"stale": true}')

    _fail_unlink_for(session_paths.FILENAME_SUMMARY_JSON, monkeypatch=monkeypatch)

    result = session_maintenance.absorb_session("tgt-summary-boom", "src-summary-boom")

    assert result["wavs_moved"] == 1
    assert not (rec_root / "src-summary-boom").exists()
    assert result["summary_invalidated"] is True
    assert (target / session_paths.FILENAME_SUMMARY_JSON).exists(), (
        "the unlink() genuinely failed, so the stale file must still be on disk"
    )


def test_absorb_both_transcript_and_summary_unlink_failures_still_completes(
    rec_root: Path, monkeypatch: pytest.MonkeyPatch
):
    """Both best-effort branches failing at once (the compounding case)
    must still leave the merge complete rather than raising on the second
    failure after having already swallowed the first."""
    target = seed_session(rec_root, "tgt-both-boom", ["20260101T000000Z__alice__abc.wav"])
    seed_session(rec_root, "src-both-boom", ["20260101T010000Z__alice__def.wav"])
    (target / session_paths.FILENAME_TRANSCRIPT_JSON).write_text('{"stale": true}')
    (target / session_paths.FILENAME_SUMMARY_JSON).write_text('{"stale": true}')

    _fail_unlink_for(
        session_paths.FILENAME_TRANSCRIPT_JSON,
        session_paths.FILENAME_SUMMARY_JSON,
        monkeypatch=monkeypatch,
    )

    result = session_maintenance.absorb_session("tgt-both-boom", "src-both-boom")

    assert result["wavs_moved"] == 1
    assert result["transcript_invalidated"] is True
    assert result["summary_invalidated"] is True
    assert not (rec_root / "src-both-boom").exists()
