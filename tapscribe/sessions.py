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

`stripped/` is a sibling subfolder containing silence-trimmed region clips
with fresh uuid8-anchored filenames; `strip-meta.json` maps each clip back
to its owning original, so per-WAV transcript caches stay isolated
between the two sources.
"""

from __future__ import annotations

import hashlib
import json
import os
import os.path
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import tapscribe.strip_meta as strip_meta
import tapscribe.voices as voices

from . import config
from .audio import wav_duration_s
from .name_resolution import DEFAULT_KNOWN_NAMES_LIMIT, known_names, resolve_session_names
from .people import PeopleRegistry
from .roster import coerce_roster, read_roster
from .session_paths import (
    DIRNAME_STRIPPED,
    FILENAME_META_JSON,
    FILENAME_ROSTER_JSON,
    FILENAME_STRIP_META_JSON,
    FILENAME_SUMMARY_JSON,
    FILENAME_TRANSCRIPT_JSON,
    FILENAME_VOICES_JSON,
    SessionPathError,
    create_session_dir,
    resolve_session_dir,
    resolve_wav,
    session_meta_path,
    stripped_dir,
)
from .text import (
    SUMMARY_SOURCES,
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
# Domain errors — FastAPI-free validation exceptions.
# ---------------------------------------------------------------------------


class MetaValidationError(Exception):
    """Invalid session metadata (bad language, oversize field, unknown summary_source)."""


# ---------------------------------------------------------------------------
# Per-session metadata (label / aliases / prompt-hotwords overrides, and the
# #84 per-session summarizer override: summary_source + summary_prompt)
# ---------------------------------------------------------------------------

_META_STRING_FIELDS = ("label", "prompt", "hotwords", "summary_source", "summary_prompt")

# The per-session summarizer source override validates against the SAME
# `SUMMARY_SOURCES` allowlist as the global default's writer ("" = no
# override). Per-source fields (command/model) are global-only by design,
# so they're not meta fields at all.


def _coerce_aliases(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(v, str)}


def _coerce_languages(value: Any) -> list[str]:
    """Normalise a stored `languages` value to lowercased ISO codes, in order,
    dropping non-strings and duplicates. Lenient by design — the write path
    already validated against the catalog, and the resolution layer
    (`_effective_candidate_languages`) re-filters, so a hand-edited junk code
    is harmless here. Anything but a list reads as "no override"."""
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(v.strip().lower() for v in value if isinstance(v, str) and v.strip()))


def _coerce_voices(value: Any) -> dict[str, dict[str, str]]:
    """The operator's Voice→Person map (ADR-0021):
    `identity#<voice> → {person_id, run_id}`. Junk entries drop."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, entry in value.items():
        if not isinstance(key, str) or not key or not isinstance(entry, dict):
            continue
        person_id, run_id = entry.get("person_id"), entry.get("run_id")
        if isinstance(person_id, str) and person_id:
            out[key] = {"person_id": person_id, "run_id": run_id if isinstance(run_id, str) else ""}
    return out


def _coerce_session_meta(raw: Any) -> dict[str, Any]:
    """Coerce a raw session-meta dict into the standard shape: string-field
    projection, alias coercion, language normalisation. Shared by
    `read_session_meta` (the uncached write-path caller) and the cached
    path in `_describe_session` so both produce the identical result."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {k: raw[k] for k in _META_STRING_FIELDS if isinstance(raw.get(k), str)}
    if isinstance(raw.get("aliases"), dict):
        out["aliases"] = _coerce_aliases(raw["aliases"])
    langs = _coerce_languages(raw.get("languages"))
    if langs:
        out["languages"] = langs
    # Must mirror `write_session_meta`'s allowlist: this projection is what the
    # READ path emits, so widening only the writer stores a `voices` map that is
    # then stripped on every read — silently, with nothing failing.
    voices_map = _coerce_voices(raw.get("voices"))
    if voices_map:
        out["voices"] = voices_map
    return out


def _read_roster_cached(sd: Path) -> dict[str, dict[str, Any]]:
    """session-roster.json through the stat-sig cache, coerced via the shared
    `roster.coerce_roster` so the cached poll path and the uncached `read_roster`
    write path produce the identical shape. {} on None/non-dict."""
    return coerce_roster(_read_session_json_cached(sd / FILENAME_ROSTER_JSON))


def read_session_meta(session: str) -> dict[str, Any]:
    """Return the per-session metadata dict: operator-editable display
    label, speaker aliases, and per-session batch prompt/hotwords
    overrides. Missing or unreadable → {} (caller can treat as no
    overrides). Non-string fields are dropped silently."""
    return _coerce_session_meta(_read_json_or_none(session_meta_path(session)))


def write_session_meta(session: str, meta: dict[str, Any]) -> None:
    """Persist the per-session meta. Partial updates (e.g. only
    `{"prompt": "..."}`) preserve existing fields the caller didn't
    mention — otherwise editing one field would clear the others.

    `prompt` and `hotwords` run through the same MAX_CONFIG_TEXT_LEN cap
    as the global config writers — symmetric with `PUT /api/config/{key}`
    so a buggy client can't bypass the guardrail via this endpoint.
    Raises `MetaValidationError` (mapped to HTTP 400) on oversize input.

    Atomic via `atomic_write_text` so a crashed write never leaves a
    torn JSON file (which `_read_json_or_none` would silently swallow,
    losing the operator's label + aliases + overrides all at once)."""
    session_dir = create_session_dir(session)
    existing = read_session_meta(session)
    allowed = {"aliases", "languages", "voices", *_META_STRING_FIELDS}
    merged = {**existing, **{k: v for k, v in meta.items() if k in allowed}}
    sanitized = {k: merged[k] if isinstance(merged.get(k), str) else "" for k in _META_STRING_FIELDS}
    sanitized["aliases"] = _coerce_aliases(merged.get("aliases"))
    voices_map = _coerce_voices(merged.get("voices"))
    if voices_map:
        sanitized["voices"] = voices_map
    # The per-meeting candidate-language override (ADR-0010) is a list, not a
    # string field. Validate every code against the catalog at WRITE time (like
    # the global config/languages writer) so a junk code can't reach the
    # pipeline's per-region run via this endpoint.
    languages = _coerce_languages(merged.get("languages"))
    if languages:
        from .transcribers.catalog import is_candidate_language

        for code in languages:
            if not is_candidate_language(code):
                raise MetaValidationError(f"unknown language code: {code!r} (not in the catalog)")
        sanitized["languages"] = languages
    for capped_field in ("prompt", "hotwords", "summary_prompt"):
        try:
            validate_config_text(sanitized[capped_field])
        except ValueError as e:
            raise MetaValidationError(str(e)) from e
    if sanitized["summary_source"] not in SUMMARY_SOURCES:
        raise MetaValidationError(
            f"unknown summary_source: {sanitized['summary_source']!r} "
            f"(expected one of: {', '.join(s for s in SUMMARY_SOURCES if s)} — or '' to clear)",
        )
    atomic_write_text(
        session_dir / FILENAME_META_JSON,
        json.dumps(sanitized, indent=2, ensure_ascii=False),
    )


def _resolution_inputs(session: str) -> dict[str, Any] | None:
    """The reads both name-resolution wrappers below need, done once and in one
    place so they cannot drift on WHICH inputs they pass (a wrapper that forgets
    `voices` silently stops naming mapped Voices). `None` when the session dir
    has vanished — a concurrent delete after the transcript read.

    Named `voices`/`voice_runs` per ADR-0021: the operator's mapping off
    session-meta, and each identity's current run from the sidecar. They must
    agree or the mapping predates a re-diarize; `resolve_session_names` owns
    that rule."""
    try:
        session_dir = resolve_session_dir(session)
    except SessionPathError:
        return None
    meta = read_session_meta(session)
    return {
        "roster": read_roster(session_dir),
        "aliases": meta.get("aliases") or {},
        "registry": PeopleRegistry.load(),
        "voices": meta.get("voices") or {},
        "voice_runs": voices.run_ids(_read_json_or_none(session_dir / FILENAME_VOICES_JSON)),
    }


def speaker_names_for_session(session: str, *, speaker_keys: Iterable[str] = ()) -> dict[str, str]:
    """`speaker key -> display name` for `session` — the SAME map `/api/state`
    layers over the transcript pane, for a server-side reader that has no poll.

    The summarize path is that reader: the stored `plain_text` carries raw keys
    (`Them#A`) by design, so a summary generated from it would name nobody the
    operator mapped. Resolving here is what makes a Voice→Person mapping reach
    the summary, and it must be the same resolution the pane shows or the two
    disagree about who spoke.

    Best-effort like its sibling: a vanished session dir degrades to `{}` (the
    raw keys), never a failed summarize."""
    inputs = _resolution_inputs(session)
    if inputs is None:
        return {}
    return resolve_session_names(**inputs, speaker_keys=speaker_keys)


def known_names_for_session(
    session: str,
    *,
    speaker_keys: Iterable[str] = (),
    limit: int = DEFAULT_KNOWN_NAMES_LIMIT,
) -> list[str]:
    """The known-people display names to hint a summarize of `session` (the
    `tapscribe.summarizers.build_names_hint` input): this session's participants
    first, then people the People Registry has learned across previous meetings.

    The I/O wrapper around the pure `name_resolution.known_names` — reads the
    session's roster + alias overrides + the registry, the same roster→Person
    join `resolve_session_names` runs at `/api/state` build time. (It does not
    replay the dashboard's ADR-0009 F1 slug backfill for old rosterless
    recordings — that needs the WAV-derived speaker list `attach_people` has on
    hand and this cold path does not; a named Person still surfaces via the
    registry tail, only its participants-first priority is lost.)

    Best-effort: names are a quality boost, not a correctness requirement, so a
    missing session dir (a concurrent delete after the transcript read) degrades
    to no hint — the pre-feature behaviour — rather than failing the summarize.
    The underlying reads already swallow torn/missing sidecars (`read_roster` /
    `read_session_meta` → `{}`, `PeopleRegistry.load` → empty)."""
    inputs = _resolution_inputs(session)
    if inputs is None:
        # The session dir vanished between the merged-transcript read and now.
        # No names to inject; the summarize proceeds unhinted rather than
        # 404-ing on an optional enrichment.
        return []
    return known_names(
        **inputs,
        limit=limit,
        # Passed in, not re-read: the summarize path has the merged transcript
        # open already, and it is hundreds of KB on a long session.
        speaker_keys=speaker_keys,
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
    data = _read_json_or_none(session_dir / FILENAME_TRANSCRIPT_JSON)
    return data if isinstance(data, dict) else None


def read_session_summary(session: str) -> dict[str, Any] | None:
    """The FULL persisted session-summary.json for `session`, or None when the
    session has never been summarized. Backs `GET /api/sessions/{session}/
    summary`. Same path-safety shape as `read_session_transcript`:
    `resolve_session_dir` validates traversal, `_read_json_or_none` re-checks
    containment at the point of file access."""
    session_dir = resolve_session_dir(session)
    data = _read_json_or_none(session_dir / FILENAME_SUMMARY_JSON)
    return data if isinstance(data, dict) else None


def write_session_summary(session: str, summary: dict[str, Any]) -> None:
    """Persist the generated summary next to the merged transcript. Atomic via
    `atomic_write_text` so a crashed write never leaves a torn JSON file that
    `_read_json_or_none` would silently swallow. One current summary per
    session — a re-generate overwrites."""
    session_dir = resolve_session_dir(session)
    atomic_write_text(
        session_dir / FILENAME_SUMMARY_JSON,
        json.dumps(summary, indent=2, ensure_ascii=False),
    )


def read_wav_transcript(session: str, name: str, source: str = "original") -> dict[str, Any] | None:
    """The FULL primary cached transcript (raw sidecar dict) for one WAV, or
    None. Backs the per-WAV lazy expand. `resolve_wav` validates session +
    name + source and returns a path proven to live under RECORDINGS_DIR, so
    `read_primary_payload` only ever opens a contained sidecar."""
    wav_path = resolve_wav(session, name, source)
    return read_primary_payload(wav_path)


valid_strip_meta = strip_meta.valid_strip_meta
strip_meta_owner_by_clip = strip_meta.strip_meta_owner_by_clip
read_strip_meta = strip_meta.read_strip_meta


def read_wav_strip_meta(session: str, name: str) -> dict[str, Any] | None:
    """The committed strip-silence cut for one ORIGINAL wav — the explicit
    {name, start_s, end_s} spans the last strip run wrote (plus its knobs
    and run stamp), or None when the session has no strip-meta or this wav
    produced no regions. Entries are fingerprinted against the original's
    current size/mtime, so spans for a since-rewritten WAV read as absent
    instead of drawing a stale cut. `resolve_wav` validates session + name."""
    wav_path = resolve_wav(session, name, "original")
    meta = read_strip_meta(stripped_dir(session))
    if meta is None:
        return None
    entry = meta["files"].get(name)
    if not isinstance(entry, dict) or not entry.get("spans"):
        return None
    # The sidecar persists only (mtime_ns, size) — slice the inode off the
    # live signature; inodes are never stored (they don't survive a copy).
    sig = file_stat_sig(wav_path)
    if sig is None or sig[:2] != (entry.get("wav_mtime_ns"), entry.get("wav_size")):
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
# str(path) -> ((mtime_ns, size) | None, parsed-json). For the per-session JSON
# sidecars the poll re-reads: session-transcript.json, session-summary.json,
# session-meta.json, session-roster.json, and stripped/strip-meta.json.
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


def _read_strip_meta_cached(stripped: Path) -> dict[str, Any] | None:
    """`read_strip_meta` memoised on (mtime_ns, size), for the once-per-second
    session listing (`build_session_files` runs every tick per session), so an
    un-cached re-parse of a static strip-meta.json is pure poll-path waste.
    Shares `_SESSION_JSON_CACHE` with the other sidecars; a re-strip rewrites the
    file (new signature) and invalidates. `gather_sessions` keeps the path in the
    prune set so the entry survives across ticks."""
    return valid_strip_meta(_read_session_json_cached(stripped / FILENAME_STRIP_META_JSON))


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


def build_session_files(
    sd: Path,
    *,
    visited: set[str] | None = None,
    open_wavs: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The per-session WAV listing: each original WAV descriptor with its
    strip-silence region clips attached as `regions`, plus the directory-level
    stripped summary (None when there's no stripped/ content).

    Shared by the once-per-second poll (`_describe_session`, for the aggregates
    + `files_sig`) and the lazy `GET /api/sessions/{s}/files` endpoint
    (`read_session_files`), so both build the EXACT same per-file shape from the
    one cached `_describe_wav`. `/api/state` no longer embeds this array — see
    `_describe_session`.

    `visited`, when supplied by `gather_sessions`, accumulates the str(path)
    of every WAV described so the per-WAV cache can be pruned to the on-disk
    set after the walk. Direct callers (the endpoint, tests) may omit it.

    `open_wavs` is the set of WAV filenames a tap is writing right now; each
    descriptor carries `open` so the dashboard can refuse to play one (its
    RIFF/data-size header is patched only at tap close, so the bytes on disk
    declare a length that isn't there yet — ADR-0017). The stamp lands on the
    per-walk copy `_describe_wav` returns, NOT on the memoised descriptor: an
    open WAV is the one file whose stats churn every tick, and a cached `open`
    would outlive the tap and disable playback forever."""
    originals = sorted(sd.glob("*.wav"))
    if visited is not None:
        visited.update(str(w) for w in originals)
    open_wavs = open_wavs or set()
    wavs = [_describe_wav(w) for w in originals]
    for w in wavs:
        w["open"] = w["name"] in open_wavs

    # Attach each original WAV's strip-silence region clips as sub-rows.
    #
    # The committed cut in stripped/strip-meta.json (schema v2) names the exact
    # clip files produced from each ORIGINAL (`files[<orig>].spans[].name`),
    # which is the only unambiguous original->region mapping. `strip_one_wav`
    # mints every region name via build_recorder_wav_name(origin_start + offset,
    # speaker, ident, fresh uuid8), so the (speaker, ident) pair is preserved but
    # is NOT unique per original: one participant who reconnects mid-session has
    # several originals sharing a single (speaker, ident), differing only by
    # timestamp and uuid. Bucketing by that pair alone would attach EVERY region
    # of EVERY same-ident original to each row, so a short recording renders as
    # dozens of clips, many longer than the file itself. So prefer the sidecar,
    # and fall back to (speaker, ident) bucketing for clips it does not name
    # (legacy stripped/ folders that predate the sidecar) or whose owning
    # original was deleted without a region cascade (`delete_session_wav` leaves
    # stripped/ intact): that keeps an orphaned clip visible under a surviving
    # same-participant sibling instead of vanishing.
    region_buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    regions_by_original: dict[str, list[dict[str, Any]]] = {}
    owner_by_clip: dict[str, str] = {}
    original_names = {w["name"] for w in wavs}
    stripped_root = sd / DIRNAME_STRIPPED
    if stripped_root.is_dir():
        # Which original each region clip was cut from, per the committed cut.
        meta = _read_strip_meta_cached(stripped_root)
        if meta is not None:
            owner_by_clip = strip_meta_owner_by_clip(meta)
        # Sorted glob keeps region rows in filename (chronological) order.
        for rw in sorted(stripped_root.glob("*.wav")):
            if visited is not None:
                visited.add(str(rw))
            desc = _describe_wav(rw)
            region_buckets.setdefault(parse_wav_speaker_ident(rw.name), []).append(desc)
            # Attach by owner only when that original still exists; a clip whose
            # owner was deleted falls through to the (speaker, ident) fallback.
            owner = owner_by_clip.get(rw.name)
            if owner in original_names:
                regions_by_original.setdefault(owner, []).append(desc)
    for w in wavs:
        # A row shows the clips the sidecar attributes to it, plus any
        # same-(speaker, ident) clip with no still-present owner (a legacy clip
        # the sidecar never named, or one orphaned by a deleted original). The
        # two sets are disjoint (an owned clip's owner is present, so it never
        # matches the fallback test), so a clip with a present owner shows under
        # that owner alone; only genuinely unattributable clips fall back to the
        # bucket. Sorted so rows stay in filename (chronological) order.
        key = parse_wav_speaker_ident(w["name"])
        owned = regions_by_original.get(w["name"], [])
        orphaned = [
            r for r in region_buckets.get(key, []) if owner_by_clip.get(r["name"]) not in original_names
        ]
        w["regions"] = sorted(owned + orphaned, key=lambda r: r["name"])
    return wavs, _stripped_summary(stripped_root, region_buckets)


def _files_signature(
    wavs: list[dict[str, Any]], stripped: dict[str, Any] | None, open_wavs: set[str] | None = None
) -> str:
    """Deterministic digest of a session's file listing. Flips whenever a WAV
    (or a stripped region) is added / removed / re-recorded, a transcript is
    (re)written, or the strip output changes — every field the dashboard's WAV
    list renders is folded in, so a cached `files[]` can't survive a real
    change. The dashboard carries this on `/api/state` and refetches the lazy
    `GET /api/sessions/{s}/files` only when it changes.

    A plain SHA-1 over the inputs (not `id()`/`hash()`) so the same on-disk
    state yields the same signature across a server restart — a client holding a
    cached list reconnects without a needless refetch, and never misses one.
    Returns "" for a session with no files at all, which is the dashboard's cue
    to render an empty list WITHOUT fetching the listing (the same contract as a
    not-yet-materialised session).

    `open_wavs` is the set of WAV filenames a tap is actively writing right now.
    A recording WAV's on-disk size grows every poll tick, so folding it in would
    flip this signature ~2 Hz for the whole meeting and make the dashboard
    refetch GET /files (and the peaks endpoint) on every tick. For an open WAV we
    substitute a stable placeholder for its size; the finalized size folds in
    once the tap closes and the WAV leaves the open set — flipping the signature
    exactly once. Mirrors the spine's deliberate duration-exclusion."""
    if not wavs and not stripped:
        return ""
    open_wavs = open_wavs or set()
    # A plain content checksum, NOT a security digest — usedforsecurity=False
    # says so (and satisfies bandit B324, which flags bare sha1 as weak crypto).
    h = hashlib.sha1(usedforsecurity=False)

    def feed(*parts: object) -> None:
        for p in parts:
            h.update(str(p).encode("utf-8"))
            h.update(b"\x1f")

    for w in wavs:
        tx = w.get("transcript") or {}
        # The PRIMARY transcript stamp covers re-transcribes; the variant COUNT
        # covers a non-primary cached variant being added/removed without moving
        # the primary (the Transcript stage's cache panel lists every variant).
        size = "OPEN" if w["name"] in open_wavs else w["size"]
        feed("w", w["name"], size, tx.get("transcribed_at") or "", len(w.get("transcripts") or []))
        for r in w.get("regions") or []:
            rtx = r.get("transcript") or {}
            feed("r", r["name"], r["size"], rtx.get("transcribed_at") or "", len(r.get("transcripts") or []))
    if stripped:
        feed("s", stripped.get("stripped_at") or "", stripped.get("count") or 0)
    return h.hexdigest()[:16]


def read_session_files(session: str, open_wavs: set[str] | None = None) -> dict[str, Any]:
    """The lazy companion to `/api/state`: the full per-session WAV listing the
    poll no longer embeds, fetched once per `files_sig` change when a session is
    opened. `resolve_session_dir` validates the id against path traversal; the
    descriptors come from the same cached `build_session_files` the poll uses.

    `open_wavs` must already be scoped to THIS session's live taps — the caller
    holds the ActiveStream snapshot and each stream names the session it writes
    into. Two sessions can legitimately hold the same filename, and an unscoped
    set would mark an idle session's WAV unplayable (ADR-0017)."""
    session_dir = resolve_session_dir(session)
    wavs, _stripped = build_session_files(session_dir, open_wavs=open_wavs)
    return {"files": wavs}


def _describe_session(
    sd: Path,
    *,
    jobs: dict[str, Any],
    current_session: str,
    visited: set[str] | None = None,
    open_wavs: set[str] | None = None,
) -> dict[str, Any]:
    """Build one entry for the dashboard's session list from `sd`.

    `visited`, when supplied by `gather_sessions`, accumulates the str(path)
    of every WAV described so the per-WAV cache can be pruned to the on-disk
    set after the walk. Direct callers (tests) may omit it.

    `open_wavs` is the set of WAV filenames a tap is currently recording; their
    growing size is kept out of `files_sig` so capture doesn't drive a per-tick
    files refetch (see `_files_signature`)."""
    wavs, stripped = build_session_files(sd, visited=visited)
    # earliest/latest reuse each descriptor's cached `wav_start` ISO (avoiding a
    # re-strptime of every WAV name per tick). Lexicographic min/max == the
    # chronological bound because `parse_wav_start` emits a fixed-width UTC
    # seconds-precision `...+00:00` string, so string order equals time order.
    wav_starts = [w["wav_start"] for w in wavs if w.get("wav_start")]
    earliest_iso = min(wav_starts) if wav_starts else None
    latest_iso = max(wav_starts) if wav_starts else None
    return {
        "session": sd.name,
        "wav_count": len(wavs),
        # files[] is NOT embedded in /api/state — the poll formerly shipped
        # EVERY session's full per-WAV array on every ~0.5s tick, which is
        # O(total WAVs) on the wire + a JSON re-parse client-side. The
        # dashboard now fetches one session's files[] lazily via
        # GET /api/sessions/{s}/files, keyed on `files_sig` below. The two
        # aggregates the listing views need (spine's total duration,
        # sessions.js's total bytes) are precomputed here so they don't have to
        # walk files[] just to sum.
        "total_bytes": sum(w["size"] for w in wavs),
        "total_duration_s": round(sum(w["duration_s"] for w in wavs), 2),
        # Distinct recorded speaker slugs (from the WAV filenames) — the People
        # view's per-session participants, which used to walk files[]. Cheap to
        # derive here since we already have the descriptors; sorted for a stable
        # poll signature.
        "speakers": sorted({w["speaker_name"] for w in wavs if w.get("speaker_name")}),
        "files_sig": _files_signature(wavs, stripped, open_wavs),
        "is_current": sd.name == current_session,
        "earliest_iso": earliest_iso,
        "latest_iso": latest_iso,
        # SLIM marker only — the full merged transcript (segments[]/plain_text/
        # suppressed[]/speaking_seconds) is fetched lazily via
        # GET /api/sessions/{session}/transcript when the session is opened.
        # The poll formerly embedded the entire (hundreds-of-KB) merged JSON for
        # EVERY session on disk on every ~0.5s tick.
        "session_transcript": _session_transcript_marker(
            _read_session_json_cached(sd / FILENAME_TRANSCRIPT_JSON)
        ),
        "session_summary": _session_summary_marker(_read_session_json_cached(sd / FILENAME_SUMMARY_JSON)),
        "progress": jobs.get(sd.name),
        "session_meta": _coerce_session_meta(_read_session_json_cached(sd / FILENAME_META_JSON)),
        # The per-session Roster (full identity → name/source/slug/wavs). Cheap
        # read, freshly built each poll like the rest of this dict, and the
        # input the People Registry + per-session name resolution join on
        # (name_resolution.attach_people, called by /api/state). Empty {} for a
        # pre-feature session, which resolves purely via its retained aliases.
        "roster": _read_roster_cached(sd),
        # Each identity's CURRENT diarization `run_id`. Name resolution compares
        # it against the run each mapping was stamped with, so a mapping made
        # before a re-diarize stops being applied (ADR-0021). Spans stay out of
        # the poll — the pane fetches those lazily.
        "voice_runs": voices.run_ids(_read_session_json_cached(sd / FILENAME_VOICES_JSON)),
        "stripped": stripped,
    }


def gather_sessions(
    *,
    current_session: str,
    jobs: dict[str, Any] | None = None,
    open_wavs: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Walk RECORDINGS_DIR and produce the dashboard's session list.

    `current_session` is the running Recorder's session ID — used to flag
    `is_current` and to synthesise an entry when the current session hasn't
    materialised on disk yet (lazy folder creation).

    `jobs` is an optional dict of session_id → job_state-dict produced by
    `recorder.jobs.snapshot()`. When present, the matching entries on each
    session get a `progress` field.

    `open_wavs` is the set of WAV filenames currently being recorded; their
    growing size is excluded from each session's `files_sig` so an in-progress
    utterance doesn't drive a per-tick files refetch (see `_files_signature`).
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
        visited_session_jsons.add(str(sd / FILENAME_TRANSCRIPT_JSON))
        visited_session_jsons.add(str(sd / FILENAME_SUMMARY_JSON))
        visited_session_jsons.add(str(sd / DIRNAME_STRIPPED / FILENAME_STRIP_META_JSON))
        visited_session_jsons.add(str(sd / FILENAME_META_JSON))
        visited_session_jsons.add(str(sd / FILENAME_ROSTER_JSON))
        visited_session_jsons.add(str(sd / FILENAME_VOICES_JSON))
        out.append(
            _describe_session(
                sd,
                jobs=jobs,
                current_session=current_session,
                visited=visited_wavs,
                open_wavs=open_wavs,
            )
        )

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
                # No folder on disk yet → no files. An empty files_sig is the
                # dashboard's cue to render an empty list WITHOUT calling the
                # lazy files endpoint (which would 404 on the missing folder).
                "total_bytes": 0,
                "total_duration_s": 0,
                "speakers": [],
                "files_sig": "",
                "is_current": True,
                "earliest_iso": None,
                "latest_iso": None,
                "session_transcript": None,
                "session_summary": None,
                "progress": None,
                "session_meta": read_session_meta(current_session),
                "roster": {},
                "stripped": None,
            },
        )
    return out


# ---------------------------------------------------------------------------
# Cross-session transcript-content search
# ---------------------------------------------------------------------------


def search_transcripts(query: str) -> list[dict[str, Any]]:
    """Scan every session's merged transcript for `query` (case-insensitive).

    Returns one hit per matching session: ``{session, label, snippet, count}``.
    Sessions without a valid merged transcript are silently skipped.

    A blank/whitespace-only query short-circuits to ``[]`` — never iterates,
    never parses, never 500.
    """
    if not query.strip():
        return []

    root = config.RECORDINGS_DIR
    results: list[dict[str, Any]] = []
    # Case-insensitive matching via regex, NOT str.lower(): lower() can change
    # a string's length ("İ".lower() is two codepoints), so indices found in
    # the lowered text misalign against the original when slicing the snippet.
    # The regex searches `plain` directly — match spans are always valid
    # snippet-window anchors.
    pattern = re.compile(re.escape(query), re.IGNORECASE)

    for sd in sorted(root.glob("*"), reverse=True):
        if not sd.is_dir():
            continue

        raw = _read_session_json_cached(sd / FILENAME_TRANSCRIPT_JSON)
        if not isinstance(raw, dict):
            continue
        plain = raw.get("plain_text")
        if not isinstance(plain, str) or not plain:
            continue

        first_match = pattern.search(plain)
        if first_match is None:
            continue

        meta = _coerce_session_meta(_read_session_json_cached(sd / FILENAME_META_JSON))
        label = meta.get("label", "")

        count = sum(1 for _ in pattern.finditer(plain))

        win_start = max(0, first_match.start() - 100)
        win_end = min(len(plain), first_match.end() + 100)
        snippet = plain[win_start:win_end]
        left_clipped = win_start > 0
        right_clipped = win_end < len(plain)
        # Trim to whole-word boundaries and mark clipping with `…`.
        if left_clipped:
            idx = snippet.find(" ")
            if idx != -1:
                snippet = snippet[idx + 1 :]
                snippet = "…" + snippet
        if right_clipped:
            idx = snippet.rfind(" ")
            if idx != -1:
                snippet = snippet[:idx]
            snippet = snippet + "…"

        results.append(
            {
                "session": sd.name,
                "label": label,
                "snippet": snippet,
                "count": count,
            }
        )

    return results
