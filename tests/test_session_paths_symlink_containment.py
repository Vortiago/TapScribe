"""Layer-2 (realpath containment) uniformity for the session-path seam (#227).

`session_paths` advertises itself as the ONE place a request-supplied `session`
/ `name` becomes a filesystem path, with a *two*-layer guard (module docstring):

1. `_safe_part` rejects separators / `.` / `..` / NUL / absolute parts, and
2. "each `resolve_*` then realpaths the candidate and confirms it stays under
   `RECORDINGS_DIR`".

Layer 1 is already pinned (test_sessions_path_safety.py). Layer 2 is NOT uniform:
`session_meta_path` and `resolve_source_dir`'s original-source branch build their
path with `_safe_part` ALONE and never realpath it. A session id that PASSES
`_safe_part` (a plain name, no separators) but is a **symlink** pointing out of
`RECORDINGS_DIR` therefore yields a path that escapes the tree — `read_session_meta`
would read a `session-meta.json` from an attacker-planted symlink target, and
`resolve_source_dir` returns a Path the module docstring promises is "proven
contained" but isn't. The realpath layer exists precisely to resolve symlinks;
these two resolvers skip it while every sibling (`resolve_session_dir`,
`stripped_dir`, `resolve_wav`) applies it.

These tests pin the harm at the containment layer: a symlinked-out session must be
REFUSED (either rejected with HTTPException(404) as the siblings do, or returned as
a realpath that stays contained) — while a legitimate session still resolves with
no false rejection, and `session_meta_path` keeps its by-design no-existence-check
semantics (`read_session_meta` returns {} for an absent session; it must not start
404-ing on one). RED today because the two resolvers leak the escaping path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from tapscribe import config, session_paths


@pytest.fixture
def recordings_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path)
    return tmp_path


def _escape_target(recordings_dir: Path) -> Path:
    """A real directory that is NOT under RECORDINGS_DIR (a sibling of it)."""
    outside = recordings_dir.parent / f"{recordings_dir.name}__escape_target"
    outside.mkdir(exist_ok=True)
    return outside


def _plant_symlink_session(recordings_dir: Path, name: str) -> None:
    """Create RECORDINGS_DIR/<name> as a symlink pointing OUT of the tree.
    `name` is a plain component that sails through `_safe_part` — the escape is
    only visible once the path is realpathed."""
    (recordings_dir / name).symlink_to(_escape_target(recordings_dir), target_is_directory=True)


def _assert_escape_refused(fn, recordings_dir: Path) -> None:
    """The seam's contract: every resolver returns a Path proven contained under
    RECORDINGS_DIR. Assert at the harm layer, mechanism-agnostic — the escape is
    refused if `fn()` either raises HTTPException(404) or returns a path whose
    realpath stays contained. Only the current behaviour (returning an escaping
    path) fails."""
    root = os.path.realpath(config.RECORDINGS_DIR)
    try:
        result = fn()
    except HTTPException as exc:
        assert exc.status_code == 404
        return
    real = os.path.realpath(result)
    assert real == root or real.startswith(root + os.sep), (
        f"seam returned an escaping path: {real!r} is not contained under {root!r}"
    )


def _assert_contained(result: Path, recordings_dir: Path) -> None:
    root = os.path.realpath(config.RECORDINGS_DIR)
    real = os.path.realpath(result)
    assert real == root or real.startswith(root + os.sep), (
        f"legitimate path not contained: {real!r} not under {root!r}"
    )


# --- harm: a symlinked-out session must not yield an escaping path ------------


def test_session_meta_path_refuses_a_symlinked_session_escape(recordings_dir):
    _plant_symlink_session(recordings_dir, "escapee")
    _assert_escape_refused(lambda: session_paths.session_meta_path("escapee"), recordings_dir)


def test_resolve_source_dir_refuses_a_symlinked_session_escape(recordings_dir):
    _plant_symlink_session(recordings_dir, "escapee")
    # original-source branch (source is None) — the one that skips the realpath layer.
    _assert_escape_refused(lambda: session_paths.resolve_source_dir("escapee", None), recordings_dir)


# --- guardrails: legitimate sessions must still resolve, no false rejection ---


def test_session_meta_path_allows_a_legitimate_session(recordings_dir):
    (recordings_dir / "20260516T130000Z").mkdir()
    result = session_paths.session_meta_path("20260516T130000Z")
    _assert_contained(result, recordings_dir)
    assert result.name == session_paths.FILENAME_META_JSON


def test_session_meta_path_does_not_require_the_session_to_exist(recordings_dir):
    # By design there is NO isdir check here (unlike resolve_session_dir):
    # read_session_meta returns {} for an absent session, so a safe-but-absent
    # session must resolve to a contained path, not start raising 404.
    result = session_paths.session_meta_path("not-created-yet")
    _assert_contained(result, recordings_dir)
    assert result.name == session_paths.FILENAME_META_JSON


def test_resolve_source_dir_allows_a_legitimate_original_source(recordings_dir):
    (recordings_dir / "20260516T130000Z").mkdir()
    result = session_paths.resolve_source_dir("20260516T130000Z", None)
    _assert_contained(result, recordings_dir)
