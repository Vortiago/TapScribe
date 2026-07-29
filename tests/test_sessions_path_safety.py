"""Regression tests for the session/name path-injection guard.

CodeQL flagged the bare `RECORDINGS_DIR / session` and `source_dir / name`
constructions in `sessions.py` because the session and name strings come
from HTTP request bodies. The fix validates each part with `_safe_part`
at the lowest path-building level — these tests pin that any string
containing a path separator, the `.` / `..` parents, an embedded NUL, or
an empty value gets rejected with 404 BEFORE the path is concatenated
(so we don't leak a file-existence oracle to attackers).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tapscribe import config, session_paths
from tapscribe.session_paths import SessionNotFound
from tapscribe.text import build_recorder_wav_name, parse_wav_speaker_ident


@pytest.fixture
def recordings_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path)
    return tmp_path


# Anything matching this should be rejected as `session` or `name`.
TRAVERSAL_INPUTS = [
    "..",
    ".",
    "",
    "../etc",
    "../../etc/passwd",
    "foo/bar",
    "foo\\bar",
    "/abs/path",
    "C:\\abs\\path",
    "foo\x00bar",
]


@pytest.mark.parametrize("bad", TRAVERSAL_INPUTS)
def test_safe_part_rejects_traversal_strings(bad):
    with pytest.raises(SessionNotFound):
        session_paths._safe_part(bad, "session")


@pytest.mark.parametrize("bad_type", [None, 42, b"bytes-not-str", object()])
def test_safe_part_rejects_non_string_types(bad_type):
    with pytest.raises(SessionNotFound):
        session_paths._safe_part(bad_type, "session")


def test_safe_part_accepts_legitimate_session_ids():
    # The format we actually emit: ISO-8601-compact UTC timestamp.
    for ok in ["20260516T130000Z", "session-1", "test_session", "a"]:
        assert session_paths._safe_part(ok, "session") == ok


@pytest.mark.parametrize("bad_session", ["..", "../escape", "foo/bar", "foo\\bar"])
def test_session_meta_path_rejects_bad_session(bad_session, recordings_dir):
    with pytest.raises(SessionNotFound):
        session_paths.session_meta_path(bad_session)


@pytest.mark.parametrize("bad_session", ["..", "../escape", "foo/bar"])
def test_resolve_session_dir_rejects_bad_session(bad_session, recordings_dir):
    with pytest.raises(SessionNotFound):
        session_paths.resolve_session_dir(bad_session)


@pytest.mark.parametrize("bad_name", ["..", "../escape.wav", "foo/bar.wav", "foo\\bar.wav"])
def test_resolve_wav_rejects_bad_name(bad_name, recordings_dir):
    # Set up a real session dir so the only thing left to fail is `name`.
    sess = recordings_dir / "20260516T130000Z"
    sess.mkdir()
    (sess / "ok.wav").write_bytes(b"")

    with pytest.raises(SessionNotFound):
        session_paths.resolve_wav("20260516T130000Z", bad_name)


# ---------------------------------------------------------------------------
# Length bound — an over-NAME_MAX component must be refused as SessionNotFound
# at layer 1, not escape the seam as a bare OSError.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        session_paths.create_session_dir,
        session_paths.session_meta_path,
        session_paths.resolve_session_dir,
        session_paths.stripped_dir,
    ],
    ids=["create_session_dir", "session_meta_path", "resolve_session_dir", "stripped_dir"],
)
def test_over_long_session_id_is_rejected_as_not_found(build, recordings_dir):  # noqa: ARG001
    """A 300-char session id passed both sanitiser layers, so `os.makedirs`
    raised `OSError: [Errno 36] File name too long` — not a `SessionPathError`,
    so not in `routes.errors.DOMAIN_ERROR_STATUS`, so `PUT /api/sessions/<300 chars>/meta`
    answered 500 while `resolve_session_dir` on the SAME id correctly raised
    SessionNotFound. Windows' 260-char MAX_PATH puts the threshold lower still."""
    with pytest.raises(SessionNotFound):
        build("a" * 300)


def test_over_long_wav_name_is_rejected_as_not_found(recordings_dir):
    sess = recordings_dir / "20260516T130000Z"
    sess.mkdir()
    with pytest.raises(SessionNotFound):
        session_paths.resolve_original_wav("20260516T130000Z", "b" * 300 + ".wav")


def test_length_bound_accepts_the_longest_name_the_recorder_can_mint():
    """The bound must clear real input by a margin. The longest WAV name the
    Recorder can mint is `build_recorder_wav_name` with a maximally long
    speaker slug (`safe_name` caps at 64) and the 10-char identity slug
    `tap_fan_out` passes (`safe_name(identity)[:10]`) — 109 chars."""
    longest = build_recorder_wav_name(datetime(2026, 5, 16, 13, 0, 0, tzinfo=UTC), "x" * 200, "y" * 10)
    assert len(longest) < 128
    assert session_paths._safe_part(longest, "file") == longest

    # batch_strip re-mints region names from an original's parsed parts, so the
    # round trip must not grow past the bound either.
    speaker, ident = parse_wav_speaker_ident(longest)
    region = build_recorder_wav_name(datetime(2026, 5, 16, 13, 0, 1, tzinfo=UTC), speaker, ident)
    assert session_paths._safe_part(region, "file") == region

    # Session ids are ISO stamps with at most a short dedup suffix.
    assert session_paths._safe_part("2026-05-16T13-00-00Z-2", "session") == "2026-05-16T13-00-00Z-2"


def test_resolve_wav_rejects_bad_session_before_touching_disk(recordings_dir):
    # Verify the guard runs upfront — we never even try to is_file() a
    # path containing `..`. Confirmed by passing a session that, if
    # concatenated, would resolve to a real file outside RECORDINGS_DIR.
    target = recordings_dir.parent / "outside.wav"
    target.write_bytes(b"")  # would be a hit for is_file() if guard misfired
    try:
        with pytest.raises(SessionNotFound):
            session_paths.resolve_wav("../" + target.parent.name, "outside.wav")
    finally:
        target.unlink()
