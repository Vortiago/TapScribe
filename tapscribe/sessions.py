"""Recording-session bookkeeping — folder layout, metadata, strip-silence.

Each backend process start creates a fresh folder under
`recordings/<UTC-timestamp>/`. Every WAV from that session lives there,
plus per-WAV transcript JSONs and any session-merged transcript.

`stripped/` is a sibling subfolder containing silence-trimmed copies of the
originals with identical filenames, so per-WAV transcript caches stay
isolated between the two sources.
"""

from __future__ import annotations

import json
import os
import os.path
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from . import config
from . import strip_silence as _ss
from .audio import wav_duration_s, wav_rms_dbfs
from .text import parse_wav_speaker_slug, parse_wav_start

if TYPE_CHECKING:
    pass


# Active-WebSockets and in-flight-job tracking now live on the Recorder
# (`recorder.streams`, `recorder.jobs`). Helpers below remain on this
# module because they're pure filesystem operations against `session_dir`
# / `stripped/` — not lifecycle state.


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

# Rejects values that would let an HTTP-supplied `session` or `name` escape
# RECORDINGS_DIR when concatenated into a path. Catches:
#   - empty strings
#   - path separators in either direction (`/`, `\`)
#   - the special directory names `.` and `..` (exact match)
#   - NUL bytes (POSIX path terminator; some platforms ignore everything after)
# Applied at the lowest path-building level so every public helper in
# this module inherits the guard rather than relying on each route to
# remember it.
_UNSAFE_PART_RE = re.compile(r"[\\/\x00]|^\.\.?$|^$")


def _safe_part(part: object, what: str = "session") -> str:
    if not isinstance(part, str) or _UNSAFE_PART_RE.search(part):
        raise HTTPException(404, f"{what} not found")
    return part


def _sanitize_under_recordings(candidate: Path | str, what: str = "not found") -> str:
    """Return the realpath of `candidate` if it sits under RECORDINGS_DIR;
    raise `HTTPException(404)` otherwise.

    Returns a STRING (the realpath) rather than a Path so that the
    sanitised value flows through CodeQL's `py/path-injection` taint
    analysis. The canonical idiom CodeQL recognises is:
        real = os.path.realpath(x)
        if os.path.commonpath([root, real]) != root: raise
        # downstream uses of `real` are sanitised
    Wrapping it behind a helper that returns a `Path` breaks that flow —
    CodeQL doesn't propagate the sanitiser property through arbitrary
    function returns or `Path()` constructors. Returning the string
    keeps the data-flow direct; callers `Path(...)` it at the public
    boundary if needed (after the sanitiser check, taint is gone)."""
    root = os.path.realpath(config.RECORDINGS_DIR)
    try:
        real = os.path.realpath(candidate)
    except (OSError, ValueError) as e:
        raise HTTPException(404, what) from e
    try:
        if os.path.commonpath([root, real]) != root:
            raise HTTPException(404, what)
    except ValueError as e:
        raise HTTPException(404, what) from e
    return real


def session_meta_path(session: str) -> Path:
    return config.RECORDINGS_DIR / _safe_part(session, "session") / "session-meta.json"


def stripped_dir(session: str) -> Path:
    candidate = config.RECORDINGS_DIR / _safe_part(session, "session") / "stripped"
    return Path(_sanitize_under_recordings(candidate, "session not found"))


def read_session_meta(session: str) -> dict[str, Any]:
    """Return the per-session metadata dict (operator-editable display label
    and speaker aliases). Missing or unreadable → {} (caller can treat as
    no overrides)."""
    data = _read_json_or_none(session_meta_path(session))
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    if isinstance(data.get("label"), str):
        out["label"] = data["label"]
    if isinstance(data.get("aliases"), dict):
        out["aliases"] = {str(k): str(v) for k, v in data["aliases"].items() if isinstance(v, str)}
    return out


def write_session_meta(session: str, meta: dict[str, Any]) -> None:
    p = session_meta_path(session)
    # Containment check BEFORE any filesystem op — keeps the path-level
    # sanitizer at the top of the function so neither mkdir nor write_text
    # ever runs on an escape path. Use the sanitized realpath downstream.
    real_parent = _sanitize_under_recordings(p.parent, "session not found")
    os.makedirs(real_parent, exist_ok=True)
    sanitized = {
        "label": meta.get("label", "") if isinstance(meta.get("label"), str) else "",
        "aliases": {str(k): str(v) for k, v in (meta.get("aliases") or {}).items() if isinstance(v, str)},
    }
    real_meta_file = os.path.join(real_parent, p.name)
    with open(real_meta_file, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(sanitized, indent=2, ensure_ascii=False))


def stripped_stats(session: str) -> dict[str, Any] | None:
    """Return directory-level info about <session>/stripped/ or None if the
    folder is missing/empty. Speech seconds is the total duration of
    everything under stripped/ (silence has already been removed)."""
    d = stripped_dir(session)
    if not d.is_dir():
        return None
    wavs = sorted(d.glob("*.wav"))
    if not wavs:
        return None
    speech = 0.0
    for w in wavs:
        try:
            speech += wav_duration_s(w)
        except OSError:
            pass
    try:
        mtime_iso = datetime.fromtimestamp(d.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        mtime_iso = None
    return {
        "count": len(wavs),
        "speech_seconds": round(speech, 2),
        "stripped_at": mtime_iso,
    }


def resolve_session_dir(session: str) -> Path:
    """Return `<RECORDINGS_DIR>/<session>` after validating it exists and
    doesn't escape RECORDINGS_DIR. Raises `HTTPException(404)` otherwise.

    This is the single seam every session-scoped route handler goes
    through — the path-traversal rule lives here, not duplicated in
    each route. Containment is checked BEFORE `is_dir()` so an attacker
    can't probe for the existence of files outside RECORDINGS_DIR."""
    candidate = config.RECORDINGS_DIR / _safe_part(session, "session")
    real = _sanitize_under_recordings(candidate, "session not found")
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
        return session_dir
    raise HTTPException(400, f"unknown source: {source!r} (expected 'original' or 'stripped')")


def resolve_wav(session: str, name: str, source: str = "original") -> Path:
    """Return the resolved WAV path under `<RECORDINGS_DIR>/<session>/...`
    after validating extension, existence, and that the resolved path
    can't escape RECORDINGS_DIR. 404 on any failure. The single seam
    every WAV-scoped route uses — duplicating the guard inline tends
    to drift between routes. Containment is checked BEFORE `is_file()`
    so an attacker can't probe for the existence of files outside
    RECORDINGS_DIR via the suffix-mismatch branch."""
    name = _safe_part(name, "file")
    source_dir = resolve_source_dir(session, source)
    real = _sanitize_under_recordings(source_dir / name, "not found")
    if not os.path.isfile(real) or not real.lower().endswith(".wav"):
        raise HTTPException(404, "not found")
    return Path(real)


# ---------------------------------------------------------------------------
# Session listing for /api/state + /sessions
# ---------------------------------------------------------------------------


def _read_json_or_none(path: Path) -> Any:
    """Parse `path` as JSON. Returns None when the file is missing,
    unparseable, or sits outside RECORDINGS_DIR — `gather_sessions`
    tolerates per-WAV transcripts going stale without breaking the
    dashboard listing.

    The containment check is defense-in-depth: every caller already
    passes a path derived from a validated session, but this second
    layer makes the safety property local and visible to static
    analysis, so a future refactor that bypasses the route-level
    validation can't silently leak the function as an arbitrary
    file-reader."""
    # Inline the realpath + commonpath sanitiser so taint analysis sees
    # the check at the point of file access, not behind a helper. Use
    # the realpath string `real` directly in subsequent os.path.* and
    # open() calls — CodeQL flows the sanitiser property through the
    # `real` variable but not through a re-wrapped Path.
    root = os.path.realpath(config.RECORDINGS_DIR)
    try:
        real = os.path.realpath(path)
        if os.path.commonpath([root, real]) != root:
            return None
    except (OSError, ValueError):
        return None
    if not os.path.isfile(real):
        return None
    try:
        with open(real, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _describe_wav(w: Path, stripped_root: Path) -> dict[str, Any]:
    """One row in the per-session `files` list — original WAV + parsed
    sidecar transcript + stripped sibling (if any)."""
    wav_start = parse_wav_start(w.name)
    dur = round(wav_duration_s(w), 2)
    wav_start_iso = wav_start.isoformat() if wav_start else None
    wav_end_iso = (wav_start + timedelta(seconds=dur)).isoformat() if wav_start else None
    # Pair the original with its stripped sibling (same filename under
    # <session>/stripped/) so the dashboard can render an indented
    # sub-row with its own transcribe button.
    stripped_sibling: dict[str, Any] | None = None
    stripped_wav = stripped_root / w.name
    if stripped_wav.is_file():
        stripped_sibling = {
            "size": stripped_wav.stat().st_size,
            "duration_s": round(wav_duration_s(stripped_wav), 2),
            "transcript": _read_json_or_none(stripped_wav.with_suffix(".json")),
        }
    return {
        "name": w.name,
        "size": w.stat().st_size,
        "duration_s": dur,
        "transcript": _read_json_or_none(w.with_suffix(".json")),
        "wav_start": wav_start_iso,
        "wav_end": wav_end_iso,
        "speaker_name": parse_wav_speaker_slug(w.name),
        "stripped": stripped_sibling,
    }


def _describe_session(
    sd: Path,
    *,
    jobs: dict[str, Any],
    current_session: str,
) -> dict[str, Any]:
    """Build one entry for the dashboard's session list from `sd`."""
    stripped_root = stripped_dir(sd.name)
    wavs = [_describe_wav(w, stripped_root) for w in sorted(sd.glob("*.wav"))]
    starts = [parse_wav_start(w["name"]) for w in wavs]
    starts = [s for s in starts if s is not None]
    earliest = min(starts) if starts else None
    latest = max(starts) if starts else None
    return {
        "session": sd.name,
        "wav_count": len(wavs),
        "files": wavs,
        "is_current": sd.name == current_session,
        "earliest_iso": earliest.isoformat() if earliest else None,
        "latest_iso": latest.isoformat() if latest else None,
        "session_transcript": _read_json_or_none(sd / "session-transcript.json"),
        "progress": jobs.get(sd.name),
        "session_meta": read_session_meta(sd.name),
        "stripped": stripped_stats(sd.name),
    }


def gather_sessions(*, current_session: str, jobs: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Walk RECORDINGS_DIR and produce the dashboard's session list.

    `current_session` is the running Recorder's session ID — used to flag
    `is_current` and to synthesise an entry when the current session hasn't
    materialised on disk yet (lazy folder creation).

    `jobs` is an optional dict of session_id → job_state-dict produced by
    `recorder.jobs.snapshot()`. When present, the matching entries on each
    session get a `progress` field.
    """
    jobs = jobs or {}
    out: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for sd in sorted(config.RECORDINGS_DIR.glob("*"), reverse=True):
        if not sd.is_dir():
            continue
        seen_names.add(sd.name)
        out.append(_describe_session(sd, jobs=jobs, current_session=current_session))

    # If the current session folder hasn't materialised on disk yet (lazy-
    # creation), the loop above missed it. Surface a synthetic entry so the
    # dashboard's sidebar always shows the current session as an anchor.
    if current_session not in seen_names:
        out.insert(
            0,
            {
                "session": current_session,
                "wav_count": 0,
                "files": [],
                "is_current": True,
                "earliest_iso": None,
                "latest_iso": None,
                "session_transcript": None,
                "progress": None,
                "session_meta": read_session_meta(current_session),
                "stripped": None,
            },
        )
    return out


# ---------------------------------------------------------------------------
# Strip-silence (operator-triggered, used by /api/sessions/{session}/strip-silence)
# ---------------------------------------------------------------------------


def strip_one_wav(
    src: Path,
    dst: Path,
    min_silence_ms: int,
    pad_ms: int,
    threshold_db: float,
    use_silero: bool,
    speech_floor_db: float,
) -> dict[str, Any]:
    """Strip silence from one WAV. Returns per-file stats. Used by the
    strip-silence endpoint, which runs this in a worker thread."""
    import numpy as np

    samples = _ss.read_wav_int16(src)
    total = len(samples)
    in_secs = total / _ss.SAMPLE_RATE
    if total == 0:
        return {
            "name": src.name,
            "in_seconds": 0.0,
            "speech_seconds": 0.0,
            "segments": 0,
            "written": False,
            "reason": "empty",
        }

    # Whole-file silence gate. Same threshold the transcribe path uses
    # (SILENT_RMS_DBFS_FLOOR). If the original WAV has no sustained signal,
    # silero will at best false-positive on a transient, and the resulting
    # stripped sibling is just concentrated noise that hallucinates under
    # Whisper. Don't write it.
    overall_rms_dbfs = wav_rms_dbfs(src)
    if overall_rms_dbfs < config.SILENT_RMS_DBFS_FLOOR:
        return {
            "name": src.name,
            "in_seconds": round(in_secs, 2),
            "speech_seconds": 0.0,
            "segments": 0,
            "written": False,
            "reason": f"whole-file silent ({overall_rms_dbfs:.1f} dBFS RMS, floor {config.SILENT_RMS_DBFS_FLOOR} dBFS)",
        }

    regions = None
    detector = "rms"
    if use_silero:
        regions = _ss.detect_speech_silero(samples, min_silence_ms=min_silence_ms, pad_ms=pad_ms)
        if regions is not None:
            detector = "silero-vad"
    if regions is None:
        regions = _ss.detect_speech_rms(
            samples,
            threshold_db=threshold_db,
            min_silence_ms=min_silence_ms,
            pad_ms=pad_ms,
        )

    if not regions:
        return {
            "name": src.name,
            "in_seconds": round(in_secs, 2),
            "speech_seconds": 0.0,
            "segments": 0,
            "written": False,
            "reason": "no speech detected",
            "detector": detector,
        }

    pre_filter_count = len(regions)
    regions = _ss.filter_low_energy_regions(samples, regions, floor_dbfs=speech_floor_db)
    if not regions:
        return {
            "name": src.name,
            "in_seconds": round(in_secs, 2),
            "speech_seconds": 0.0,
            "segments": 0,
            "written": False,
            "reason": f"all {pre_filter_count} regions below {speech_floor_db:.1f} dBFS speech floor",
            "detector": detector,
        }

    out_samples = np.concatenate([samples[s:e] for s, e in regions])
    speech_secs = len(out_samples) / _ss.SAMPLE_RATE
    dst.parent.mkdir(parents=True, exist_ok=True)
    _ss.write_wav_int16(dst, out_samples)
    return {
        "name": src.name,
        "in_seconds": round(in_secs, 2),
        "speech_seconds": round(speech_secs, 2),
        "segments": len(regions),
        "segments_filtered_below_floor": pre_filter_count - len(regions),
        "written": True,
        "detector": detector,
    }
