"""Recording-session listing + metadata — the dashboard's read model.

Walks `recordings/<session>/` to build the per-session listing the dashboard
polls (`gather_sessions`, memoised on cheap stat signatures), reads/writes the
per-session `session-meta.json` (label, aliases, prompt/hotwords overrides),
and lazily reads the full merged + per-WAV transcripts that the slim poll
markers point at.

The neighbouring concerns live in their own modules so this one stays the
once-per-second read path: path resolution + the path-safety guard are in
`session_paths`; destructive operator operations (absorb, delete, prune) are in
`session_maintenance`; the strip-silence splitter is in `batch_strip`.

`stripped/` is a sibling subfolder containing silence-trimmed copies of the
originals with identical filenames, so per-WAV transcript caches stay isolated
between the two sources.
"""

from __future__ import annotations

import json
import os
import os.path
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import config
from .audio import wav_duration_s
from .session_paths import _safe_part, resolve_session_dir, resolve_wav, session_meta_path, stripped_dir
from .text import (
    atomic_write_text,
    file_stat_sig,
    parse_wav_speaker_ident,
    parse_wav_speaker_slug,
    parse_wav_start,
    validate_config_text,
)
from .wav_cache import cache_listing, cache_signature, read_primary_marker, read_primary_payload

# Active-WebSockets and in-flight-job tracking now live on the Recorder
# (`recorder.streams`, `recorder.jobs`). Helpers below remain on this
# module because they're pure filesystem reads against `session_dir`
# / `stripped/` — not lifecycle state.


# ---------------------------------------------------------------------------
# Per-session metadata (label / aliases / prompt-hotwords overrides)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Lazy full-transcript reads (the bodies /api/state's markers no longer embed)
# ---------------------------------------------------------------------------


def read_session_transcript(session: str) -> dict[str, Any] | None:
    """The FULL merged session-transcript.json for `session`, or None when the
    session has no merged transcript. Backs `GET /api/sessions/{session}/
    transcript`. `session` is validated against path traversal by
    `resolve_session_dir` (the canonical CodeQL realpath sanitiser); the file
    is read through `_read_json_or_none`, which re-checks containment so static
    analysis sees the guard at the point of file access."""
    session_dir = resolve_session_dir(session)
    data = _read_json_or_none(session_dir / "session-transcript.json")
    return data if isinstance(data, dict) else None


def read_session_summary(session: str) -> dict[str, Any] | None:
    """The FULL persisted session-summary.json for `session`, or None when the
    session has never been summarized. Backs `GET /api/sessions/{session}/
    summary`. Same path-safety shape as `read_session_transcript`:
    `resolve_session_dir` validates traversal, `_read_json_or_none` re-checks
    containment at the point of file access."""
    session_dir = resolve_session_dir(session)
    data = _read_json_or_none(session_dir / "session-summary.json")
    return data if isinstance(data, dict) else None


def write_session_summary(session: str, summary: dict[str, Any]) -> None:
    """Persist the generated summary next to the merged transcript. Atomic via
    `atomic_write_text` so a crashed write never leaves a torn JSON file that
    `_read_json_or_none` would silently swallow. One current summary per
    session — a re-generate overwrites."""
    session_dir = resolve_session_dir(session)
    atomic_write_text(
        session_dir / "session-summary.json",
        json.dumps(summary, indent=2, ensure_ascii=False),
    )


def read_wav_transcript(session: str, name: str, source: str = "original") -> dict[str, Any] | None:
    """The FULL primary cached transcript (raw sidecar dict) for one WAV, or
    None. Backs the per-WAV lazy expand. `resolve_wav` validates session +
    name + source and returns a path proven to live under RECORDINGS_DIR, so
    `read_primary_payload` only ever opens a contained sidecar."""
    wav_path = resolve_wav(session, name, source)
    return read_primary_payload(wav_path)


def read_wav_strip_meta(session: str, name: str) -> dict[str, Any] | None:
    """The committed strip-silence cut for one ORIGINAL wav — the explicit
    {name, start_s, end_s} spans the last strip run wrote (plus its knobs
    and run stamp), or None when the session has no strip-meta or this wav
    produced no regions. Entries are fingerprinted against the original's
    current size/mtime, so spans for a since-rewritten WAV read as absent
    instead of drawing a stale cut. `resolve_wav` validates session + name;
    `_read_json_or_none` re-checks containment before opening the sidecar."""
    wav_path = resolve_wav(session, name, "original")
    meta = _read_json_or_none(stripped_dir(session) / "strip-meta.json")
    if not isinstance(meta, dict):
        return None
    entry = (meta.get("files") or {}).get(name)
    if not isinstance(entry, dict) or not entry.get("spans"):
        return None
    try:
        st = os.stat(wav_path)
    except OSError:
        return None
    if st.st_size != entry.get("wav_size") or st.st_mtime_ns != entry.get("wav_mtime_ns"):
        return None
    return {"spans": entry["spans"], "stripped_at": meta.get("stripped_at"), "knobs": meta.get("knobs")}


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


# ---------------------------------------------------------------------------
# Poll-path memoisation
# ---------------------------------------------------------------------------
#
# The dashboard polls /api/state once per second, and gather_sessions walks
# every session + every WAV on each tick. Without caching, each tick re-opens
# every WAV (header read for duration) and re-reads + re-parses every
# transcript sidecar across the whole archive — O(total WAVs) disk + JSON work
# that grows unbounded as recordings accumulate. The per-WAV descriptor and the
# (sometimes large) session-transcript.json are stable until their files
# change, so memoise each on a cheap stat signature and recompute only on a
# real change. Entries are pruned to the on-disk set at the end of every walk.
#
# Lock-free on purpose: gather_sessions runs on a worker thread and two polls
# can overlap, but every entry is keyed on a stat signature, so the worst a
# race can do is a redundant recompute — never serve a stale value.

# str(path) -> (cache_key, descriptor). The key is (wav mtime_ns, wav size,
# transcript-sidecar signature); see wav_cache.cache_signature.
_WAV_DESC_CACHE: dict[str, tuple[tuple, dict[str, Any]]] = {}
# str(path) -> ((mtime_ns, size) | None, parsed-json). For session-transcript.json.
_SESSION_JSON_CACHE: dict[str, tuple[tuple | None, Any]] = {}


def _prune_cache(cache: dict[str, Any], keep: set[str]) -> None:
    """Drop entries whose path wasn't visited in the latest walk, bounding the
    poll caches to what's actually on disk (sessions and WAVs get deleted)."""
    for key in list(cache.keys()):
        if key not in keep:
            cache.pop(key, None)


def _read_session_json_cached(path: Path) -> Any:
    """`_read_json_or_none` memoised on (mtime_ns, size). session-transcript.json
    can be hundreds of KB on a long session, so re-parsing it on every poll is
    the second-biggest poll cost after the per-WAV reads; a re-transcribe/merge
    rewrites it (new signature) and invalidates. The returned object is shared
    read-only with the JSON response serialiser."""
    pathkey = str(path)
    sig = file_stat_sig(path)
    if sig is None:
        _SESSION_JSON_CACHE.pop(pathkey, None)
        return None
    hit = _SESSION_JSON_CACHE.get(pathkey)
    if hit is not None and hit[0] == sig:
        return hit[1]
    data = _read_json_or_none(path)
    _SESSION_JSON_CACHE[pathkey] = (sig, data)
    return data


def _session_transcript_marker(data: Any) -> dict[str, Any] | None:
    """Project the full merged session-transcript.json down to the SLIM marker
    the dashboard listing reads without rendering: `transcribed_at`,
    `segment_count`, `suppressed_count`, and `speakers` (main.js builds its
    speaker-alias set from it). DROPS the heavy `segments[]`, `suppressed[]`,
    `plain_text`, and `speaking_seconds` — the dashboard fetches the full
    merged transcript lazily via `GET /api/sessions/{session}/transcript` only
    when the session is open. None when there's no merged transcript.

    A marker change (different `transcribed_at`) is the client's signal to
    re-fetch + re-render; every field a marker-consumer reads is preserved so
    sig-gates and badges keep working off the listing alone."""
    if not isinstance(data, dict):
        return None
    segments = data.get("segments")
    suppressed = data.get("suppressed")
    speakers = data.get("speakers")
    marker: dict[str, Any] = {
        "transcribed_at": data.get("transcribed_at"),
        "segment_count": len(segments) if isinstance(segments, list) else 0,
        # suppressed_count is already a top-level field on the wire shape, but
        # fall back to len(suppressed[]) for older files that predate it.
        "suppressed_count": data.get("suppressed_count")
        if isinstance(data.get("suppressed_count"), int)
        else (len(suppressed) if isinstance(suppressed, list) else 0),
        "speakers": list(speakers) if isinstance(speakers, list) else [],
    }
    return marker


def _session_summary_marker(data: Any) -> dict[str, Any] | None:
    """Project the persisted session-summary.json down to the SLIM marker the
    dashboard listing reads: `summarized_at`, `source`, `model`, and
    `transcribed_at` (the stamp of the transcript the summary was built from).
    DROPS the `summary` body and `prompt` — the dashboard fetches the full
    summary lazily via `GET /api/sessions/{session}/summary` when the Summary
    stage is open. A marker change (different `summarized_at`) is the client's
    re-fetch signal. None when the session has never been summarized."""
    if not isinstance(data, dict):
        return None
    return {
        "summarized_at": data.get("summarized_at"),
        "source": data.get("source") or "",
        "model": data.get("model") or "",
        "transcribed_at": data.get("transcribed_at"),
    }


def _describe_wav_uncached(w: Path, *, size: int) -> dict[str, Any]:
    wav_start = parse_wav_start(w.name)
    dur = round(wav_duration_s(w), 2)
    wav_start_iso = wav_start.isoformat() if wav_start else None
    wav_end_iso = (wav_start + timedelta(seconds=dur)).isoformat() if wav_start else None
    return {
        "name": w.name,
        "size": size,
        # SLIM marker only — the full transcript (segments[]/text/suppressed[])
        # is fetched lazily via GET /api/wav/{session}/{name}/transcript when a
        # row is expanded. The poll used to embed read_primary_payload(w) here
        # for EVERY WAV of EVERY session, ballooning /api/state to megabytes.
        "duration_s": dur,
        "transcript": read_primary_marker(w),
        "transcripts": cache_listing(w),
        "wav_start": wav_start_iso,
        "wav_end": wav_end_iso,
        "speaker_name": parse_wav_speaker_slug(w.name),
    }


def _describe_wav(w: Path) -> dict[str, Any]:
    """One row in the per-session `files` list — original WAV + parsed
    sidecar transcript (the primary, when multiple are cached) +
    `transcripts` listing for the picker UI. The cache reads go through
    `wav_cache.read_primary_payload` and `cache_listing` so a session
    with many WAVs doesn't re-walk each transcripts dir multiple times
    per poll tick.

    Memoised on (wav mtime_ns, wav size, transcript-sidecar signature) so a
    re-poll of an unchanged WAV does zero file opens / JSON parses. Returns a
    shallow copy so `_describe_session` can attach a `regions` list without
    polluting the cached descriptor.

    Region WAVs produced by strip-silence (in <session>/stripped/) get
    attached as `regions` by `_describe_session` — they share the same
    row shape as originals (no nested `regions` of their own)."""
    sig = file_stat_sig(w)
    if sig is None:
        # File vanished mid-walk — describe it uncached and tolerantly
        # (wav_duration_s + the sidecar reads all return empty on error).
        return _describe_wav_uncached(w, size=0)
    key = (*sig, cache_signature(w))
    pathkey = str(w)
    hit = _WAV_DESC_CACHE.get(pathkey)
    if hit is not None and hit[0] == key:
        return dict(hit[1])
    desc = _describe_wav_uncached(w, size=sig[1])
    _WAV_DESC_CACHE[pathkey] = (key, desc)
    return dict(desc)


def _stripped_summary(
    stripped_root: Path,
    region_buckets: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Directory-level <session>/stripped/ stats, derived from the region
    descriptors `_describe_session` already built (so we don't re-open every
    region WAV a second time the way the old `stripped_stats` did). None when
    there's no stripped/ content — same contract the dashboard relied on."""
    regions = [r for bucket in region_buckets.values() for r in bucket]
    if not regions:
        return None
    speech = round(sum(r["duration_s"] for r in regions), 2)
    try:
        stripped_at = datetime.fromtimestamp(stripped_root.stat().st_mtime, tz=UTC).isoformat()
    except OSError:
        stripped_at = None
    return {"count": len(regions), "speech_seconds": speech, "stripped_at": stripped_at}


def _describe_session(
    sd: Path,
    *,
    jobs: dict[str, Any],
    current_session: str,
    visited: set[str] | None = None,
) -> dict[str, Any]:
    """Build one entry for the dashboard's session list from `sd`.

    `visited`, when supplied by `gather_sessions`, accumulates the str(path)
    of every WAV described so the per-WAV cache can be pruned to the on-disk
    set after the walk. Direct callers (tests) may omit it."""
    originals = sorted(sd.glob("*.wav"))
    if visited is not None:
        visited.update(str(w) for w in originals)
    wavs = [_describe_wav(w) for w in originals]

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
            if visited is not None:
                visited.add(str(rw))
            key = parse_wav_speaker_ident(rw.name)
            region_buckets.setdefault(key, []).append(_describe_wav(rw))
    for w in wavs:
        w["regions"] = region_buckets.get(parse_wav_speaker_ident(w["name"]), [])
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
        # SLIM marker only — the full merged transcript (segments[]/plain_text/
        # suppressed[]/speaking_seconds) is fetched lazily via
        # GET /api/sessions/{session}/transcript when the session is opened.
        # The poll formerly embedded the entire (hundreds-of-KB) merged JSON for
        # EVERY session on disk on every ~0.5s tick.
        "session_transcript": _session_transcript_marker(
            _read_session_json_cached(sd / "session-transcript.json")
        ),
        "session_summary": _session_summary_marker(_read_session_json_cached(sd / "session-summary.json")),
        "progress": jobs.get(sd.name),
        "session_meta": read_session_meta(sd.name),
        "stripped": _stripped_summary(stripped_root, region_buckets),
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
    visited_wavs: set[str] = set()
    visited_session_jsons: set[str] = set()
    for sd in sorted(config.RECORDINGS_DIR.glob("*"), reverse=True):
        if not sd.is_dir():
            continue
        seen_names.add(sd.name)
        visited_session_jsons.add(str(sd / "session-transcript.json"))
        visited_session_jsons.add(str(sd / "session-summary.json"))
        out.append(_describe_session(sd, jobs=jobs, current_session=current_session, visited=visited_wavs))

    # Prune the poll caches down to what this walk actually saw so deleted
    # sessions/WAVs don't pin descriptors for the process lifetime.
    _prune_cache(_WAV_DESC_CACHE, visited_wavs)
    _prune_cache(_SESSION_JSON_CACHE, visited_session_jsons)

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
                "session_summary": None,
                "progress": None,
                "session_meta": read_session_meta(current_session),
                "stripped": None,
            },
        )
    return out
