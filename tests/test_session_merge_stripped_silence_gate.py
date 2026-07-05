"""RED contract for #240 — select_session_wavs(source="stripped")'s silence
gate must check the TRUE original (via stripped/strip-meta.json's owner
mapping), not the stripped clip itself.

session_merge.py's comment claims the gate "always reads the ORIGINAL even
when source=stripped", but it looks the original up as
`session_dir / <stripped clip name>` — and strip_one_wav mints every region
clip a fresh timestamp+uuid8 name, so that lookup never hits and the
fallback (checking the clip against itself) silently fires every time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from wav_builders import seed_silent_wav, seed_wav  # type: ignore[import-not-found]

import tapscribe.sessions as sessions
from tapscribe.session_merge import select_session_wavs

ORIGINAL_NAME = "2026-05-12T09-19-55Z_alice_ident01_00000001.wav"
CLIP_NAME = "2026-05-12T09-20-00Z_alice_ident01_ab12cd34.wav"


def _write_strip_meta(stripped_dir: Path, session_dir: Path, orig_name: str, clip_name: str) -> None:
    """Minimal v2 strip-meta.json: maps `clip_name` back to its owning
    `orig_name`, the same shape sessions.py's build_session_files reads."""
    st = (session_dir / orig_name).stat()
    (stripped_dir / "strip-meta.json").write_text(
        json.dumps(
            {
                "stripped_at": datetime.now(UTC).isoformat(),
                "knobs": {},
                "files": {
                    orig_name: {
                        "wav_size": st.st_size,
                        "wav_mtime_ns": st.st_mtime_ns,
                        "spans": [{"name": clip_name, "start_s": 0.0, "end_s": 1.0}],
                    }
                },
            }
        )
    )


def test_stripped_clip_skipped_silent_when_true_original_is_silent(tmp_path: Path):
    """Original is near-silent; the stripped clip itself happens to read
    audible. The gate must key off the ORIGINAL via strip-meta's owner
    mapping and still skip the clip as silent."""
    session_dir = tmp_path / "session"
    stripped = session_dir / "stripped"
    stripped.mkdir(parents=True)

    seed_silent_wav(session_dir / ORIGINAL_NAME)
    seed_wav(stripped / CLIP_NAME)
    _write_strip_meta(stripped, session_dir, ORIGINAL_NAME, CLIP_NAME)

    selection = select_session_wavs(session_dir, source="stripped")

    assert selection.wavs == ()
    assert CLIP_NAME in selection.skipped_silent


def test_stripped_clip_kept_when_true_original_is_audible_though_clip_itself_reads_faint(tmp_path: Path):
    """Inverse: original is audible but the clip (post-trim) reads faint.
    Since the gate must check the ORIGINAL, not the clip, it stays selected."""
    session_dir = tmp_path / "session"
    stripped = session_dir / "stripped"
    stripped.mkdir(parents=True)

    seed_wav(session_dir / ORIGINAL_NAME)
    seed_silent_wav(stripped / CLIP_NAME)
    _write_strip_meta(stripped, session_dir, ORIGINAL_NAME, CLIP_NAME)

    selection = select_session_wavs(session_dir, source="stripped")

    assert [w.name for w in selection.wavs] == [CLIP_NAME]
    assert selection.skipped_silent == ()


def test_stripped_clip_with_no_owner_mapping_falls_back_to_checking_itself(tmp_path: Path):
    """A legacy stripped/ folder with no strip-meta.json (or a clip the
    sidecar doesn't name) has no owner to look up — the gate must fall back
    to RMS-checking the clip in hand, same as today, not raise or skip the
    check entirely."""
    session_dir = tmp_path / "session"
    stripped = session_dir / "stripped"
    stripped.mkdir(parents=True)

    seed_silent_wav(stripped / CLIP_NAME)

    selection = select_session_wavs(session_dir, source="stripped")

    assert selection.wavs == ()
    assert CLIP_NAME in selection.skipped_silent


def test_stripped_clip_owner_with_path_traversal_is_rejected_not_followed(tmp_path: Path):
    """A strip-meta.json entry naming an owner outside session_dir/ (path
    traversal here; an absolute path would escape just as far) must never be
    joined into a real filesystem lookup. The gate falls back to
    RMS-checking the clip itself instead of walking outside session_dir/."""
    session_dir = tmp_path / "session"
    stripped = session_dir / "stripped"
    stripped.mkdir(parents=True)

    # An audible WAV sitting OUTSIDE session_dir/, at the traversal target.
    # If the traversal were followed, the gate would read this instead and
    # wrongly keep the (actually silent) clip selected.
    seed_wav(tmp_path / "outside.wav")
    seed_silent_wav(stripped / CLIP_NAME)
    _write_strip_meta(stripped, session_dir, "../outside.wav", CLIP_NAME)

    selection = select_session_wavs(session_dir, source="stripped")

    assert selection.wavs == ()
    assert CLIP_NAME in selection.skipped_silent


def test_sessions_docstring_no_longer_claims_identical_filenames():
    """sessions.py's module docstring described the pre-uuid8 naming scheme
    ("stripped/ ... identical filenames"); strip_one_wav has minted a fresh
    timestamp+uuid8 name per clip for a while now, so the claim is stale and
    actively misleads a reader about how a clip joins back to its original."""
    assert "identical filenames" not in (sessions.__doc__ or "")
