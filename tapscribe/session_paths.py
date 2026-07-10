"""Session path resolution — the one place a request-supplied `session` /
`name` becomes a filesystem path under `RECORDINGS_DIR`.

This is the path-safety seam. CodeQL flags `RECORDINGS_DIR / session` style
constructions because the parts come from HTTP requests; the two-layer
sanitiser lives here ONCE so every caller crosses it instead of re-deriving the
guard:

1. `_safe_part` rejects path separators, `.`/`..`, NUL, empty, and
   platform-absolute parts at the lowest path-building level.
2. each `resolve_*` then realpaths the candidate and confirms it stays under
   `RECORDINGS_DIR` (the canonical `realpath(x).startswith(root + os.sep)`
   idiom CodeQL's `py/path-injection` query recognises intraprocedurally).

Callers receive a `Path` proven contained, so downstream filesystem ops don't
re-check. New code that turns request input into a recordings path goes through
these helpers — never `config.RECORDINGS_DIR / <raw>` by hand.
"""

from __future__ import annotations

import os
import os.path
import re
from pathlib import Path

from fastapi import HTTPException

from . import config

# Rejects values that would let an HTTP-supplied `session` or `name` escape
# RECORDINGS_DIR when concatenated into a path. Catches:
#   - empty strings
#   - path separators in either direction (`/`, `\`)
#   - the special directory names `.` and `..` (exact match)
#   - NUL bytes (POSIX path terminator; some platforms ignore everything after)
# Applied at the lowest path-building level so every public helper here inherits
# the guard rather than relying on each route to remember it.
_UNSAFE_PART_RE = re.compile(r"[\\/\x00]|^\.\.?$|^$")


def _safe_part(part: object, what: str = "session") -> str:
    if not isinstance(part, str) or _UNSAFE_PART_RE.search(part):
        raise HTTPException(404, f"{what} not found")
    # Defense-in-depth: pathlib's `/` operator treats an absolute argument
    # as overriding the parent — `Path("D:/rec") / "C:foo"` is `C:foo` on
    # Windows, escaping RECORDINGS_DIR entirely. The regex above catches
    # `/`, `\`, and NUL, but on Windows the drive prefix `C:` doesn't go
    # through any of those. Reject anything pathlib would consider
    # absolute on the current platform.
    if Path(part).is_absolute():
        raise HTTPException(404, f"{what} not found")
    return part


def _assert_contained(candidate: Path) -> None:
    """Layer 2: confirm `candidate` stays under RECORDINGS_DIR after
    symlink resolution. Raises HTTPException(404) on escape."""
    root = os.path.realpath(config.RECORDINGS_DIR)
    real = os.path.realpath(candidate)
    if real != root and not real.startswith(root + os.sep):
        raise HTTPException(404, "session not found")


# ---------------------------------------------------------------------------
# On-disk session layout — the canonical name for each per-session bookkeeping
# file and the stripped/ subdir. The ONE owner of each literal: every reader,
# writer, and maintenance op composes these onto an already-resolved session
# (or stripped) dir instead of hand-typing the string, so a rename touches one
# line. Distinct from the `source == "stripped"` API selector value in
# resolve_source_dir below — that's a wire enum, not a path component, and the
# two are free to diverge.
# ---------------------------------------------------------------------------
FILENAME_TRANSCRIPT_JSON = "session-transcript.json"
FILENAME_TRANSCRIPT_TXT = "session-transcript.txt"
FILENAME_SUMMARY_JSON = "session-summary.json"
FILENAME_META_JSON = "session-meta.json"
FILENAME_ROSTER_JSON = "session-roster.json"
FILENAME_STRIP_META_JSON = "strip-meta.json"
DIRNAME_STRIPPED = "stripped"


def session_meta_path(session: str) -> Path:
    path = config.RECORDINGS_DIR / _safe_part(session, "session") / FILENAME_META_JSON
    _assert_contained(path)
    return path


def stripped_dir(session: str) -> Path:
    """Build `<RECORDINGS_DIR>/<session>/stripped` after validating the
    session id against path traversal. The realpath+startswith check is
    inlined (the canonical CodeQL-recognised idiom for `py/path-injection`)
    so callers receive a Path that downstream filesystem operations can
    use without re-checking."""
    session = _safe_part(session, "session")
    root = os.path.realpath(config.RECORDINGS_DIR)
    real = os.path.realpath(os.path.join(root, session, DIRNAME_STRIPPED))
    if real != root and not real.startswith(root + os.sep):
        raise HTTPException(404, "session not found")
    return Path(real)


def resolve_session_dir(session: str) -> Path:
    """Return `<RECORDINGS_DIR>/<session>` after validating it exists and
    doesn't escape RECORDINGS_DIR. Raises `HTTPException(404)` otherwise.

    Uses the canonical `os.path.realpath(x).startswith(root + os.sep)`
    sanitiser pattern that CodeQL's `py/path-injection` query recognises
    intraprocedurally."""
    session = _safe_part(session, "session")
    root = os.path.realpath(config.RECORDINGS_DIR)
    real = os.path.realpath(os.path.join(root, session))
    if real != root and not real.startswith(root + os.sep):
        raise HTTPException(404, "session not found")
    if not os.path.isdir(real):
        raise HTTPException(404, "session not found")
    return Path(real)


def resolve_source_dir(session: str, source: str | None) -> Path:
    """Pick the WAV folder for a transcribe request.

    source == 'stripped' → <session>/stripped/  (must exist)
    source in (None, '', 'original') → <session>/
    """
    session = _safe_part(session, "session")
    session_dir = config.RECORDINGS_DIR / session
    if source == "stripped":
        d = stripped_dir(session)
        if not d.is_dir():
            raise HTTPException(404, "stripped/ not found for this session; run strip-silence first")
        return d
    if source in (None, "", "original"):
        _assert_contained(session_dir)
        return session_dir
    raise HTTPException(400, f"unknown source: {source!r} (expected 'original' or 'stripped')")


def create_session_dir(session: str) -> Path:
    """Return `<RECORDINGS_DIR>/<session>` validated with both layers,
    creating the directory if it does not yet exist."""
    session = _safe_part(session, "session")
    session_dir = config.RECORDINGS_DIR / session
    _assert_contained(session_dir)
    os.makedirs(session_dir, exist_ok=True)
    return session_dir


def resolve_original_wav(session: str, name: str) -> Path:
    """Return the ORIGINAL WAV path for `session` / `name` with both
    containment layers applied. Does NOT check existence (the caller
    handles missing originals)."""
    session = _safe_part(session, "session")
    name = _safe_part(name, "file")
    path = config.RECORDINGS_DIR / session / name
    _assert_contained(path)
    return path


def resolve_wav(session: str, name: str, source: str = "original") -> Path:
    """Return the resolved WAV path under `<RECORDINGS_DIR>/<session>/...`
    after validating extension, existence, and that the resolved path
    can't escape RECORDINGS_DIR. 404 on any failure.

    Uses the canonical `os.path.realpath(x).startswith(root + os.sep)`
    sanitiser pattern recognised by CodeQL's `py/path-injection` query."""
    name = _safe_part(name, "file")
    source_dir = resolve_source_dir(session, source)
    root = os.path.realpath(config.RECORDINGS_DIR)
    real = os.path.realpath(os.path.join(str(source_dir), name))
    if real != root and not real.startswith(root + os.sep):
        raise HTTPException(404, "not found")
    if not os.path.isfile(real) or not real.lower().endswith(".wav"):
        raise HTTPException(404, "not found")
    return Path(real)
