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

import pytest
from fastapi import HTTPException

from tapscribe import config, session_paths


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
    with pytest.raises(HTTPException) as exc:
        session_paths._safe_part(bad, "session")
    assert exc.value.status_code == 404


@pytest.mark.parametrize("bad_type", [None, 42, b"bytes-not-str", object()])
def test_safe_part_rejects_non_string_types(bad_type):
    with pytest.raises(HTTPException) as exc:
        session_paths._safe_part(bad_type, "session")
    assert exc.value.status_code == 404


def test_safe_part_accepts_legitimate_session_ids():
    # The format we actually emit: ISO-8601-compact UTC timestamp.
    for ok in ["20260516T130000Z", "session-1", "test_session", "a"]:
        assert session_paths._safe_part(ok, "session") == ok


@pytest.mark.parametrize("bad_session", ["..", "../escape", "foo/bar", "foo\\bar"])
def test_session_meta_path_rejects_bad_session(bad_session, recordings_dir):
    with pytest.raises(HTTPException) as exc:
        session_paths.session_meta_path(bad_session)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("bad_session", ["..", "../escape", "foo/bar"])
def test_resolve_session_dir_rejects_bad_session(bad_session, recordings_dir):
    with pytest.raises(HTTPException) as exc:
        session_paths.resolve_session_dir(bad_session)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("bad_name", ["..", "../escape.wav", "foo/bar.wav", "foo\\bar.wav"])
def test_resolve_wav_rejects_bad_name(bad_name, recordings_dir):
    # Set up a real session dir so the only thing left to fail is `name`.
    sess = recordings_dir / "20260516T130000Z"
    sess.mkdir()
    (sess / "ok.wav").write_bytes(b"")

    with pytest.raises(HTTPException) as exc:
        session_paths.resolve_wav("20260516T130000Z", bad_name)
    assert exc.value.status_code == 404


def test_resolve_wav_rejects_bad_session_before_touching_disk(recordings_dir):
    # Verify the guard runs upfront — we never even try to is_file() a
    # path containing `..`. Confirmed by passing a session that, if
    # concatenated, would resolve to a real file outside RECORDINGS_DIR.
    target = recordings_dir.parent / "outside.wav"
    target.write_bytes(b"")  # would be a hit for is_file() if guard misfired
    try:
        with pytest.raises(HTTPException) as exc:
            session_paths.resolve_wav("../" + target.parent.name, "outside.wav")
        assert exc.value.status_code == 404
    finally:
        target.unlink()
