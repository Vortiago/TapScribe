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
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from . import config
from . import strip_silence as _ss
from .audio import wav_duration_s, wav_rms_dbfs
from .text import (
    atomic_write_text,
    build_recorder_wav_name,
    parse_wav_speaker_slug,
    parse_wav_start,
    validate_config_text,
)
from .wav_cache import cache_listing, read_primary_payload

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
    # Defense-in-depth: pathlib's `/` operator treats an absolute argument
    # as overriding the parent — `Path("D:/rec") / "C:foo"` is `C:foo` on
    # Windows, escaping RECORDINGS_DIR entirely. The regex above catches
    # `/`, `\`, and NUL, but on Windows the drive prefix `C:` doesn't go
    # through any of those. Reject anything pathlib would consider
    # absolute on the current platform.
    if Path(part).is_absolute():
        raise HTTPException(404, f"{what} not found")
    return part


def session_meta_path(session: str) -> Path:
    return config.RECORDINGS_DIR / _safe_part(session, "session") / "session-meta.json"


def stripped_dir(session: str) -> Path:
    """Build `<RECORDINGS_DIR>/<session>/stripped` after validating the
    session id against path traversal. The realpath+startswith check is
    inlined (the canonical CodeQL-recognised idiom for `py/path-injection`)
    so callers receive a Path that downstream filesystem operations can
    use without re-checking."""
    session = _safe_part(session, "session")
    root = os.path.realpath(config.RECORDINGS_DIR)
    real = os.path.realpath(os.path.join(root, session, "stripped"))
    if real != root and not real.startswith(root + os.sep):
        raise HTTPException(404, "session not found")
    return Path(real)


_META_STRING_FIELDS = ("label", "prompt", "hotwords")


def _coerce_aliases(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(v, str)}


def read_session_meta(session: str) -> dict[str, Any]:
    """Return the per-session metadata dict: operator-editable display
    label, speaker aliases, and per-session batch prompt/hotwords
    overrides. Missing or unreadable → {} (caller can treat as no
    overrides). Non-string fields are dropped silently."""
    data = _read_json_or_none(session_meta_path(session))
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {k: data[k] for k in _META_STRING_FIELDS if isinstance(data.get(k), str)}
    if isinstance(data.get("aliases"), dict):
        out["aliases"] = _coerce_aliases(data["aliases"])
    return out


def write_session_meta(session: str, meta: dict[str, Any]) -> None:
    """Persist the per-session meta. Partial updates (e.g. only
    `{"prompt": "..."}`) preserve existing fields the caller didn't
    mention — otherwise editing one field would clear the others.

    `prompt` and `hotwords` run through the same MAX_CONFIG_TEXT_LEN cap
    as the global config writers — symmetric with `PUT /api/config/{key}`
    so a buggy client can't bypass the guardrail via this endpoint.
    Raises `HTTPException(400)` on oversize input.

    Atomic via `atomic_write_text` so a crashed write never leaves a
    torn JSON file (which `_read_json_or_none` would silently swallow,
    losing the operator's label + aliases + overrides all at once)."""
    session = _safe_part(session, "session")
    root = os.path.realpath(config.RECORDINGS_DIR)
    real_parent = os.path.realpath(os.path.join(root, session))
    if real_parent != root and not real_parent.startswith(root + os.sep):
        raise HTTPException(404, "session not found")
    os.makedirs(real_parent, exist_ok=True)
    existing = read_session_meta(session)
    allowed = {"aliases", *_META_STRING_FIELDS}
    merged = {**existing, **{k: v for k, v in meta.items() if k in allowed}}
    sanitized = {k: merged[k] if isinstance(merged.get(k), str) else "" for k in _META_STRING_FIELDS}
    sanitized["aliases"] = _coerce_aliases(merged.get("aliases"))
    for capped_field in ("prompt", "hotwords"):
        try:
            validate_config_text(sanitized[capped_field])
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    atomic_write_text(
        Path(real_parent) / "session-meta.json",
        json.dumps(sanitized, indent=2, ensure_ascii=False),
    )


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
        mtime_iso = datetime.fromtimestamp(d.stat().st_mtime, tz=UTC).isoformat()
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
        return session_dir
    raise HTTPException(400, f"unknown source: {source!r} (expected 'original' or 'stripped')")


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
    # Inline the realpath + startswith sanitiser (canonical CodeQL
    # `py/path-injection` form) so taint analysis sees the check at the
    # point of file access. Use the realpath string `real` directly in
    # subsequent os.path.* and open() calls — CodeQL flows the sanitiser
    # property through the `real` variable but not through a re-wrapped Path.
    root = os.path.realpath(config.RECORDINGS_DIR)
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return None
    if real != root and not real.startswith(root + os.sep):
        return None
    if not os.path.isfile(real):
        return None
    try:
        with open(real, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _describe_wav(w: Path) -> dict[str, Any]:
    """One row in the per-session `files` list — original WAV + parsed
    sidecar transcript (the primary, when multiple are cached) +
    `transcripts` listing for the picker UI. The cache reads go through
    `wav_cache.read_primary_payload` and `cache_listing` so a session
    with many WAVs doesn't re-walk each transcripts dir multiple times
    per poll tick.

    Region WAVs produced by strip-silence (in <session>/stripped/) get
    attached as `regions` by `_describe_session` — they share the same
    row shape as originals (no nested `regions` of their own)."""
    wav_start = parse_wav_start(w.name)
    dur = round(wav_duration_s(w), 2)
    wav_start_iso = wav_start.isoformat() if wav_start else None
    wav_end_iso = (wav_start + timedelta(seconds=dur)).isoformat() if wav_start else None
    return {
        "name": w.name,
        "size": w.stat().st_size,
        "duration_s": dur,
        "transcript": read_primary_payload(w),
        "transcripts": cache_listing(w),
        "wav_start": wav_start_iso,
        "wav_end": wav_end_iso,
        "speaker_name": parse_wav_speaker_slug(w.name),
    }


def _describe_session(
    sd: Path,
    *,
    jobs: dict[str, Any],
    current_session: str,
) -> dict[str, Any]:
    """Build one entry for the dashboard's session list from `sd`."""
    wavs = [_describe_wav(w) for w in sorted(sd.glob("*.wav"))]

    # Bucket region WAVs from stripped/ by (speaker_slug, ident) so each
    # original WAV's row can render the N regions it was split into as
    # sub-rows. strip_one_wav mints region names via build_recorder_wav_name(
    # origin_start + offset, original_speaker_slug, original_ident, fresh
    # uuid8), so the (speaker, ident) pair survives the split and identifies
    # the source unambiguously. Regions from legacy stripped/ folders
    # (pre-split refactor) bucket against their originals the same way —
    # those filenames preserved the same convention.
    region_buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    stripped_root = sd / "stripped"
    if stripped_root.is_dir():
        for rw in sorted(stripped_root.glob("*.wav")):
            key = _split_filename_components(rw.name)
            region_buckets.setdefault(key, []).append(_describe_wav(rw))
    for w in wavs:
        w["regions"] = region_buckets.get(_split_filename_components(w["name"]), [])
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


def session_is_empty(session_dir: Path) -> bool:
    """True when a session folder holds nothing worth keeping: no WAVs, no
    merged transcript, and no operator label.

    The single definition of "empty session", shared by `prune_empty_sessions`
    (which folders to delete) and the tap new-session idempotency guard
    (whether rotating would just churn an untouched session) — so "empty"
    means the same thing in both places.
    """
    if any(session_dir.glob("*.wav")):
        return False
    if (session_dir / "session-transcript.json").exists():
        return False
    if read_session_meta(session_dir.name).get("label"):
        return False
    return True


def prune_empty_sessions(current_session: str) -> dict[str, Any]:
    """Delete every session folder under RECORDINGS_DIR that `session_is_empty`
    (no WAVs, no merged transcript, no operator label). Never deletes
    `current_session`. Returns ``{"pruned": [...], "count": N, "failed": [...]}``.

    Pure filesystem walk over the ``RECORDINGS_DIR`` glob (a constant): no path
    is built from request input, so the module's path-injection guard doesn't
    apply here. Shared by the manual `/api/sessions/prune-empty` endpoint and
    the rotate-then-prune flow behind every new-session trigger.
    """
    pruned: list[str] = []
    failed: list[dict[str, str]] = []
    for sd in config.RECORDINGS_DIR.glob("*"):
        # Skip symlinks BEFORE is_dir() (which follows them): a symlink planted
        # in RECORDINGS_DIR must never let this delete an out-of-tree target.
        # (shutil.rmtree also refuses symlinks, but don't rely on that internal.)
        if sd.is_symlink():
            continue
        if not sd.is_dir():
            continue
        if sd.name == current_session:
            continue
        if not session_is_empty(sd):
            continue
        try:
            shutil.rmtree(sd)
            pruned.append(sd.name)
        except OSError as e:
            # Log the raw OSError (it embeds the filesystem path + errno)
            # server-side only — this dict flows out of the tap-facing
            # /api/tap/new-session response, so surfacing it would leak
            # internals (CodeQL py/stack-trace-exposure). Return a generic
            # marker; the operator reads the real cause in the server log.
            print(f"[tapscribe] prune failed for {sd.name}: {e}", flush=True)
            failed.append({"session": sd.name, "error": "delete failed"})
    return {"pruned": pruned, "count": len(pruned), "failed": failed}


# ---------------------------------------------------------------------------
# Strip-silence (operator-triggered, used by /api/sessions/{session}/strip-silence)
# ---------------------------------------------------------------------------


def strip_one_wav(
    src: Path,
    out_dir: Path,
    min_silence_ms: int,
    pad_ms: int,
    speech_floor_db: float,
) -> dict[str, Any]:
    """Split one WAV into one output per detected speech region.

    Each region's output filename uses the recorder's naming convention
    `<iso>_<speaker>_<ident>_<uuid>.wav` with `<iso>` recomputed as
    `original_start + region_start_seconds`. That makes `parse_wav_start`
    place each region at its true wall-clock time during the session
    merge with no extra metadata.

    Used by the strip-silence endpoint, which runs this in a worker thread.
    """

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
            "regions_written": [],
            "reason": "empty",
        }

    # Whole-file silence gate. Same threshold the transcribe path uses
    # (SILENT_RMS_DBFS_FLOOR). If the original WAV has no sustained signal,
    # silero will at best false-positive on a transient, and the per-region
    # outputs are just concentrated noise that hallucinates under Whisper.
    # Don't write them.
    overall_rms_dbfs = wav_rms_dbfs(src)
    if overall_rms_dbfs < config.SILENT_RMS_DBFS_FLOOR:
        return {
            "name": src.name,
            "in_seconds": round(in_secs, 2),
            "speech_seconds": 0.0,
            "segments": 0,
            "written": False,
            "regions_written": [],
            "reason": f"whole-file silent ({overall_rms_dbfs:.1f} dBFS RMS, floor {config.SILENT_RMS_DBFS_FLOOR} dBFS)",
        }

    regions = _ss.detect_speech_silero(samples, min_silence_ms=min_silence_ms, pad_ms=pad_ms)

    if not regions:
        return {
            "name": src.name,
            "in_seconds": round(in_secs, 2),
            "speech_seconds": 0.0,
            "segments": 0,
            "written": False,
            "regions_written": [],
            "reason": "no speech detected",
            "detector": "silero-vad",
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
            "regions_written": [],
            "reason": f"all {pre_filter_count} regions below {speech_floor_db:.1f} dBFS speech floor",
            "detector": "silero-vad",
        }

    out_dir.mkdir(parents=True, exist_ok=True)
    origin = parse_wav_start(src.name) or datetime.fromtimestamp(src.stat().st_mtime, tz=UTC)
    speaker_slug, ident_slug = _split_filename_components(src.name)

    speech_samples = 0
    regions_written: list[str] = []
    for start_sample, end_sample in regions:
        region_samples = samples[start_sample:end_sample]
        offset_s = start_sample / _ss.SAMPLE_RATE
        region_start = origin + timedelta(seconds=offset_s)
        fname = build_recorder_wav_name(region_start, speaker_slug, ident_slug)
        _ss.write_wav_int16(out_dir / fname, region_samples)
        regions_written.append(fname)
        speech_samples += len(region_samples)

    return {
        "name": src.name,
        "in_seconds": round(in_secs, 2),
        "speech_seconds": round(speech_samples / _ss.SAMPLE_RATE, 2),
        "segments": len(regions),
        "segments_filtered_below_floor": pre_filter_count - len(regions),
        "written": True,
        "regions_written": regions_written,
        "detector": "silero-vad",
    }


def _split_filename_components(name: str) -> tuple[str, str]:
    """Pull `<speaker_slug>` and `<ident>` out of a recorder filename so
    we can stitch them back into the per-region output names.

    Falls back to safe defaults when the input doesn't follow the
    `<iso>_<speaker_slug>_<ident>_<utt>.wav` convention so a hand-dropped
    WAV still produces workable output names.
    """
    speaker = parse_wav_speaker_slug(name) or "anon"
    stem = name.rsplit(".", 1)[0]
    parts = stem.split("_")
    ident = parts[-2] if len(parts) >= 4 else "unknown"
    return speaker, ident


# ---------------------------------------------------------------------------
# Session absorb — fold one session's WAVs into another
# ---------------------------------------------------------------------------


def _move_sidecars_with_wav(src_wav: Path, dst_wav: Path) -> None:
    """Carry whichever cache layout the source uses to the WAV's new
    home — the legacy `<wav>.json` file, the new `<wav>.transcripts/`
    directory, or both. Either may be absent."""
    legacy = src_wav.with_suffix(".json")
    if legacy.is_file():
        shutil.move(str(legacy), str(dst_wav.with_suffix(".json")))
    transcripts = src_wav.with_suffix(".transcripts")
    if transcripts.is_dir():
        shutil.move(str(transcripts), str(dst_wav.with_suffix(".transcripts")))


def _safe_size(p: Path) -> int:
    """Size of `p` in bytes, or 0 if it isn't a file or can't be statted.
    The `bytes_freed` the delete endpoints report is advisory, so a stat
    race (a path vanishing mid-walk) must never abort the delete."""
    try:
        return p.stat().st_size if p.is_file() else 0
    except OSError:
        return 0


def _dir_size(d: Path) -> int:
    """Best-effort sum of file sizes under `d`, for the delete endpoints'
    advisory `bytes_freed`."""
    return sum(_safe_size(f) for f in d.rglob("*"))


def _delete_wav_with_sidecars(wav: Path) -> int:
    """Delete one WAV plus whichever transcript-cache layout it carries —
    the legacy `<wav>.json` file, the new `<wav>.transcripts/` directory,
    or both. Returns the bytes reclaimed (WAV + sidecars).

    The destructive analog of `_move_sidecars_with_wav` above: both
    enumerate the SAME two sidecar layouts, so if a third layout is ever
    added, update both functions.

    `wav` is a Path discovered by globbing a `resolve_session_dir`-checked
    folder (or returned by `resolve_wav`), not a path BUILT from a parsed
    filename — so the `safe_name` round-trip rule (which guards path
    construction against `py/path-injection`) does not apply here; deleting
    an already-validated Path is safe."""
    freed = _safe_size(wav)
    legacy = wav.with_suffix(".json")
    if legacy.is_file():
        freed += _safe_size(legacy)
        legacy.unlink(missing_ok=True)
    transcripts = wav.with_suffix(".transcripts")
    if transcripts.is_dir():
        freed += _dir_size(transcripts)
        # Best-effort: a locked cache dir shouldn't block reclaiming the
        # WAV; an orphaned cache dir is harmless and re-cleanable.
        shutil.rmtree(transcripts, ignore_errors=True)
    wav.unlink(missing_ok=True)
    return freed


def absorb_session(target: str, source: str) -> dict[str, Any]:
    """Move every WAV (and its `<name>.json` sidecar) from `source` into
    `target`, fold the source's `session-meta.json` aliases into the
    target's (target wins on key conflict), invalidate the target's merged
    transcript, and delete the source folder.

    Per-WAV filenames embed timestamps + UUIDs so cross-session collisions
    are essentially impossible, but we still refuse the merge rather than
    silently overwrite if any do collide.

    Pre-flight checks the route handler performs first (current-session
    refusal, in-flight-job refusal) live in the route; this function is
    purely the filesystem operation. Raises `HTTPException` on validation
    failures so the route doesn't have to translate exceptions.
    """
    if target == source:
        raise HTTPException(400, "target and source must differ")

    target_dir = resolve_session_dir(target)
    source_dir = resolve_session_dir(source)

    src_wavs = sorted(source_dir.glob("*.wav"))
    src_stripped_dir = source_dir / "stripped"
    src_stripped_wavs = sorted(src_stripped_dir.glob("*.wav")) if src_stripped_dir.is_dir() else []

    # Refuse the merge rather than silently overwrite. Caller can rename
    # the colliding files manually if they really mean it.
    collisions: list[str] = []
    for w in src_wavs:
        if (target_dir / w.name).exists():
            collisions.append(w.name)
    tgt_stripped_dir = target_dir / "stripped"
    for w in src_stripped_wavs:
        if (tgt_stripped_dir / w.name).exists():
            collisions.append(f"stripped/{w.name}")
    if collisions:
        raise HTTPException(409, f"filename collision(s) between sessions: {', '.join(collisions[:5])}")

    moved_wavs: list[str] = []
    moved_stripped: list[str] = []

    # Move originals + their sidecars. A WAV may carry either a legacy
    # `<wav>.json` (one transcript) or a `<wav>.transcripts/` directory
    # (one per cached backend+model); both layouts must follow the WAV.
    for w in src_wavs:
        shutil.move(str(w), str(target_dir / w.name))
        _move_sidecars_with_wav(w, target_dir / w.name)
        moved_wavs.append(w.name)

    # Move stripped/ siblings. If the target has no stripped/ yet, create
    # it; if both sides have stripped/, files merge in (the collision check
    # above already guarantees no overwrites).
    if src_stripped_wavs:
        tgt_stripped_dir.mkdir(parents=True, exist_ok=True)
        for w in src_stripped_wavs:
            shutil.move(str(w), str(tgt_stripped_dir / w.name))
            _move_sidecars_with_wav(w, tgt_stripped_dir / w.name)
            moved_stripped.append(w.name)

    # Merge speaker aliases. Target wins on conflict; source fills in keys
    # the target doesn't already have. Target's label is preserved as-is.
    tgt_meta = read_session_meta(target)
    src_meta = read_session_meta(source)
    src_aliases = src_meta.get("aliases") or {}
    tgt_aliases = dict(tgt_meta.get("aliases") or {})
    aliases_added: list[str] = []
    for k, v in src_aliases.items():
        if k not in tgt_aliases:
            tgt_aliases[k] = v
            aliases_added.append(k)
    if aliases_added or tgt_meta:
        write_session_meta(
            target,
            {
                "label": tgt_meta.get("label", "") or "",
                "aliases": tgt_aliases,
            },
        )

    # The target's merged transcript predates the just-moved WAVs, so it's
    # now stale. Drop it so the operator's next "transcribe whole session"
    # rebuilds against the fuller WAV set.
    tgt_transcript = target_dir / "session-transcript.json"
    transcript_invalidated = tgt_transcript.exists()
    if transcript_invalidated:
        try:
            tgt_transcript.unlink()
        except OSError:
            # Best-effort: the WAVs are already moved at this point, so
            # raising would leave the merge half-applied (WAVs in target,
            # source still on disk, transcript still stale). The transcript
            # will be overwritten on the next ▶ transcribe whole session.
            pass

    # Source folder is now expected to hold only metadata files (session-meta,
    # session-transcript) and an empty stripped/ at most. Wipe the whole tree.
    shutil.rmtree(source_dir)

    return {
        "target": target,
        "source": source,
        "wavs_moved": len(moved_wavs),
        "stripped_moved": len(moved_stripped),
        "aliases_added": aliases_added,
        "transcript_invalidated": transcript_invalidated,
    }


# ---------------------------------------------------------------------------
# Audio deletion (operator-triggered, used by the delete endpoints)
# ---------------------------------------------------------------------------


def delete_session_audio(session: str) -> dict[str, Any]:
    """Delete ALL of a session's audio to reclaim disk: every original WAV
    in `<session>/`, the entire `<session>/stripped/` folder, and each
    WAV's per-WAV transcript-cache sidecars. KEEPS the merged
    `session-transcript.json` / `.txt` and `session-meta.json`, so the
    session survives `prune-empty` and the operator's result + label are
    preserved.

    Route-level guards (current-session refusal, in-flight-job refusal)
    live in the handler; this is purely the filesystem op. Returns
    `{session, wavs_deleted, bytes_freed}` — `wavs_deleted` counts the
    originals (matching the dashboard's "N wavs" header); `stripped/`
    bytes still fold into `bytes_freed`."""
    session_dir = resolve_session_dir(session)
    wavs_deleted = 0
    bytes_freed = 0
    for w in sorted(session_dir.glob("*.wav")):
        bytes_freed += _delete_wav_with_sidecars(w)
        wavs_deleted += 1
    stripped = session_dir / "stripped"
    if stripped.is_dir():
        bytes_freed += _dir_size(stripped)
        try:
            shutil.rmtree(stripped)
        except OSError as e:
            raise HTTPException(500, f"delete failed: {e}") from None
    return {"session": session, "wavs_deleted": wavs_deleted, "bytes_freed": bytes_freed}


def delete_session_wav(session: str, name: str, source: str = "original") -> dict[str, Any]:
    """Delete a single WAV (validated via `resolve_wav`) plus its sidecars.

    No region cascade: deleting an original does NOT sweep its derived
    `stripped/` regions, because regions bucket on `(speaker_slug, ident)`
    which is not unique per original (multiple WAVs from one tap identity
    share it), so a cascade could over-delete a sibling original's regions.
    Use the bulk `delete_session_audio` to free everything.

    Returns `{session, name, source, bytes_freed}`."""
    wav = resolve_wav(session, name, source)
    bytes_freed = _delete_wav_with_sidecars(wav)
    return {"session": session, "name": name, "source": source, "bytes_freed": bytes_freed}
