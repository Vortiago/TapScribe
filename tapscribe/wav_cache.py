"""Per-WAV transcription cache.

Each transcribed WAV gets one or more cached transcripts in a sibling
`<wav>.transcripts/` directory — one sidecar JSON per (backend, model),
plus a `_primary` text file pointing at the entry the merge layer
reads. `cached_transcribe` is the policy-aware entry point: cache hit
returns the parsed sidecar for this transcriber's (backend, model),
miss runs the Transcriber + hallucination filter and writes a new entry
without evicting other entries.

Legacy `<wav>.json` sidecars (one transcript per WAV) are still
readable; the first new-layout write for the same WAV migrates the
legacy file into the new layout so the two formats never coexist.

The on-disk JSON wire shape inside each sidecar is unchanged: a
TranscriptionResult flattened with the write-time envelope (when this
WAV was transcribed, source folder, parsed speaker, absolute UTC start
time) and the on-disk WAV fingerprint (size + mtime). This module owns
the layout; CONTEXT.md "Per-WAV transcript cache" is the term.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import hallucinations as hallucinations_mod
from .text import atomic_write_text, parse_iso, parse_wav_speaker_slug, parse_wav_start
from .transcribers.base import (
    ConstrainedLanguageDetector,
    Transcriber,
    TranscriptionResult,
    TranscriptionSegment,
)


@dataclass(frozen=True)
class CachedTranscription:
    """The on-disk per-WAV JSON, parsed: a TranscriptionResult plus the
    write-time envelope (when, source folder, parsed speaker, wav_start)
    and the on-disk WAV fingerprint (size + mtime) we use to detect that
    the WAV was rewritten since the transcript was produced — the resume
    path rewrites the same path with appended audio, so the cache key
    must be more than just (backend, model)."""

    result: TranscriptionResult
    transcribed_at: datetime
    transcribe_ms: int
    source: str
    wav_start: datetime | None
    speaker_name: str
    wav_size: int = 0
    wav_mtime_ns: int = 0
    # Fingerprint of the hallucination rules the stored kept/suppressed split
    # was decided under (see `_rules_fingerprint`). None on a legacy entry
    # written before the field existed — those refilter once, then self-heal.
    rules_sig: str | None = None


# ---------------------------------------------------------------------------
# Path helpers — on-disk layout for the multi-transcript cache
# ---------------------------------------------------------------------------

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_PRIMARY_POINTER = "_primary"


def _safe(component: str) -> str:
    """Sanitise one filename component to `[A-Za-z0-9._-]+`, replacing
    other characters with `-`. Empty input becomes `_` so the filename
    always has a body."""
    if not component:
        return "_"
    return _FILENAME_SAFE_RE.sub("-", component)


def _entry_key(backend: str, model: str) -> str:
    """Build the per-entry index key: `<backend>__<model>` after
    sanitising each component."""
    return f"{_safe(backend)}__{_safe(model)}"


def transcripts_dir(wav_path: Path) -> Path:
    """The WAV's new-layout `<wav>.transcripts/` sidecar directory (one
    entry per cached backend+model). The canonical definition of the
    layout — `session_maintenance`'s move/delete helpers consume it via
    `sidecar_paths` below."""
    return wav_path.with_suffix(".transcripts")


def legacy_sidecar(wav_path: Path) -> Path:
    """The WAV's legacy single-transcript `<wav>.json` sidecar. See
    `transcripts_dir` for the one-owner rationale."""
    return wav_path.with_suffix(".json")


def sidecar_paths(wav_path: Path) -> tuple[tuple[str, Path], ...]:
    """EVERY sidecar location `wav_path` MAY carry, as `(kind, path)`
    pairs — the single enumeration of "all of a WAV's sidecars".

    `kind` is the entry's filesystem shape, `"file"` or `"dir"`, which is
    exactly what a consumer needs to pick its per-entry action (stat+unlink
    vs walk+rmtree; a move handles both). Entries may be absent on disk —
    callers check existence per their kind. Order is stable, so a caller
    mapping source→destination (`_move_sidecars_with_wav`) can zip two
    calls together.

    This is the seam that keeps the layout extendable in ONE file:
    `session_maintenance._move_sidecars_with_wav` and
    `_delete_wav_with_sidecars` iterate this tuple, so a THIRD cache
    layout added here is automatically carried on absorb-move and
    counted + removed on delete / bulk-reclaim — nothing to wire by hand
    in another module."""
    return (
        ("file", legacy_sidecar(wav_path)),
        ("dir", transcripts_dir(wav_path)),
    )


def _resolve_sidecar_paths(wav_path: Path) -> tuple[Path, ...]:
    """Return all sidecar JSON paths that exist on disk for a WAV, in deterministic order.

    Checks ``transcripts_dir(wav_path).is_dir()`` once — if it exists, returns
    ``sorted(d.glob("*.json"))``; otherwise returns ``(legacy_path,)`` if the legacy
    file exists, or ``()`` if neither layout has anything."""
    d = transcripts_dir(wav_path)
    if d.is_dir():
        return tuple(sorted(d.glob("*.json")))
    legacy = legacy_sidecar(wav_path)
    return (legacy,) if legacy.is_file() else ()


def _primary_path_of(wav_path: Path, raw_paths: tuple[Path, ...]) -> Path | None:
    """The primary sidecar PATH among ``raw_paths`` (a WAV's live-layout
    sidecars, as returned by ``_resolve_sidecar_paths``), or ``None`` when
    there are none.

    Parse-free: resolves the primary path WITHOUT reading any sidecar body.
    New layout honors the ``_primary`` pointer with a newest-mtime fallback;
    the legacy layout's single sidecar is the primary by definition."""
    if not raw_paths:
        return None
    d = transcripts_dir(wav_path)
    if d.is_dir():
        name = _primary_filename(d, list(raw_paths))
        return d / name if name else None
    # Legacy-only: the single sidecar is the primary.
    return raw_paths[0]


def _resolve_sidecars(
    wav_path: Path,
) -> tuple[list[CachedTranscription], int]:
    """Return ``(entries, primary_index)`` for a WAV's sidecars.

    * ``entries`` — parsed ``CachedTranscription`` for each surviving
      (parseable) path from ``_resolve_sidecar_paths``, in the same order.
    * ``primary_index`` — index of the primary within *entries*, or ``-1``
      when the primary didn't survive parsing.

    Primary is resolved over the **full** ``_resolve_sidecar_paths`` result
    (parse-agnostic) via ``_primary_path_of``."""
    raw_paths = _resolve_sidecar_paths(wav_path)
    if not raw_paths:
        return ([], -1)

    primary_path = _primary_path_of(wav_path, raw_paths)

    # Parse and filter: surviving entries in same order as raw_paths.
    entries: list[CachedTranscription] = []
    primary_index = -1
    for path in raw_paths:
        entry = _read_entry(path)
        if entry is not None:
            entries.append(entry)
            if primary_path is not None and path == primary_path:
                primary_index = len(entries) - 1

    return (entries, primary_index)


def cache_signature(wav_path: Path) -> tuple:
    """A cheap, stat-only signature of this WAV's cached transcripts, so the
    dashboard listing can memoise `read_primary_payload` / `cache_listing`
    across the once-per-second /api/state poll without re-reading + re-parsing
    every sidecar each tick.

    Combines the `<wav>.transcripts/` directory mtime — which bumps when a
    sidecar is *added or removed* — with the `_primary` pointer file's mtime,
    which `_write_entry` rewrites on *every* transcribe (including an in-place
    re-transcribe of the same (backend, model), which overwrites the sidecar
    without touching the directory mtime) and `set_primary_transcript`
    rewrites on every re-point. Together they catch every change that can
    alter what the dashboard shows. Falls back to the legacy `<wav>.json`
    mtime when the new-layout directory doesn't exist; legacy sidecars are
    immutable once migrated, so their mtime alone is a sufficient signature.

    Relies on a re-transcribe's write landing on a later mtime than the
    previous one — safe in practice because a real transcribe runs a model for
    far longer than any filesystem's mtime granularity (~15 ms on Windows)
    before writing the sidecar.
    """
    d = transcripts_dir(wav_path)
    try:
        dir_mtime = d.stat().st_mtime_ns
    except OSError:
        try:
            return ("legacy", legacy_sidecar(wav_path).stat().st_mtime_ns)
        except OSError:
            return ("none",)
    try:
        primary_mtime = (d / _PRIMARY_POINTER).stat().st_mtime_ns
    except OSError:
        primary_mtime = 0
    return ("dir", dir_mtime, primary_mtime)


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


def read_cached(wav_path: Path) -> CachedTranscription | None:
    """Return the *primary* cached transcript for `wav_path`, or None.

    The primary is whichever entry the `_primary` pointer names; if the
    pointer is missing or stale, we fall back to the newest-mtime
    sidecar. When only a legacy `<wav>.json` exists, it is the primary
    by definition."""
    target = _primary_sidecar_path(wav_path)
    if target is None:
        return None
    return _read_entry(target)


def read_primary_payload(wav_path: Path) -> dict[str, Any] | None:
    """Return the primary transcript as the raw on-disk JSON dict, or
    None. Bypasses the `CachedTranscription` dataclass build so the
    dashboard hot path can stream sidecars to the wire without an
    intermediate parse/serialize round-trip."""
    target = _primary_sidecar_path(wav_path)
    if target is None:
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_primary_marker(wav_path: Path) -> dict[str, Any] | None:
    """A SLIM marker for the primary transcript — the small subset of fields
    the dashboard listing reads without rendering the transcript body.

    Returns `{"transcribed_at", "transcribe_ms", "backend", "model",
    "source", "segment_count"}` (omitting any that are absent) or None when no
    transcript is cached. Used by the `/api/state` poll so the per-WAV row can
    show its has-transcript marker, "took Xms" cell, and the set-primary
    compare key (backend/model/source) WITHOUT embedding the full segments[] /
    text / suppressed[] payload — the dashboard fetches that lazily via
    `GET /api/wav/{session}/{name}/transcript` only when a row is expanded.

    Parses the same sidecar `read_primary_payload` would, then projects the
    marker fields — one read, one parse, like the full read it replaces."""
    data = read_primary_payload(wav_path)
    if data is None:
        return None
    marker: dict[str, Any] = {}
    for key in ("transcribed_at", "backend", "model", "source"):
        val = data.get(key)
        if val is not None:
            marker[key] = val
    transcribe_ms = data.get("transcribe_ms")
    if transcribe_ms is not None:
        marker["transcribe_ms"] = transcribe_ms
    segments = data.get("segments")
    if isinstance(segments, list):
        marker["segment_count"] = len(segments)
    return marker


def read_all_cached(wav_path: Path) -> list[CachedTranscription]:
    """Every cached transcript for `wav_path`, one per (backend, model).
    Unparseable sidecars are silently dropped. Order follows filesystem listing.

    Reads through the layout seam (`_resolve_sidecar_paths`) and parses each
    surviving sidecar — no primary pointer is resolved, since this listing
    doesn't distinguish the primary."""
    out: list[CachedTranscription] = []
    for path in _resolve_sidecar_paths(wav_path):
        entry = _read_entry(path)
        if entry is not None:
            out.append(entry)
    return out


def cache_listing(wav_path: Path) -> list[dict[str, Any]]:
    """Compact per-(backend, model) listing for dashboards. One walk,
    one parse per entry: returns `{"backend", "model", "source",
    "is_primary", "transcribe_ms"?}` dicts ready for the wire. `source`
    ("original"|"stripped") is what the entry was transcribed from — the
    dashboard's set-primary needs it to resolve the file's directory, since a
    stripped clip lives in <session>/stripped/. Single-sidecar legacy WAVs
    return a one-element list with `is_primary=True` when the sidecar parses."""
    entries, primary_idx = _resolve_sidecars(wav_path)
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(entries):
        item: dict[str, Any] = {
            "backend": entry.result.backend,
            "model": entry.result.model,
            "source": entry.source,
            "is_primary": i == primary_idx,
        }
        if entry.transcribe_ms:
            item["transcribe_ms"] = entry.transcribe_ms
        out.append(item)
    return out


def set_primary_transcript(wav_path: Path, *, backend: str, model: str) -> None:
    """Point the primary at the named `(backend, model)` entry. Raises
    `FileNotFoundError` if that entry isn't cached for this WAV.

    Implicitly migrates the legacy sidecar layout into the new one if
    necessary so the pointer has somewhere to live."""
    _migrate_legacy_if_needed(wav_path)
    d = transcripts_dir(wav_path)
    key = _entry_key(backend, model)
    target = d / f"{key}.json"
    if not target.is_file():
        raise FileNotFoundError(f"no cached transcript for backend={backend!r}, model={model!r} at {target}")
    atomic_write_text(d / _PRIMARY_POINTER, key)


# ---------------------------------------------------------------------------
# Cache-aware transcribe
# ---------------------------------------------------------------------------


def cached_transcribe(
    wav_path: Path,
    transcriber: Transcriber,
    *,
    initial_prompt: str | None,
    hotwords: str | None,
    hallucination_rules: list[dict[str, Any]],
    source_lang: str | None = None,
    candidate_languages: tuple[str, ...] = (),
    force: bool = False,
    source: str = "original",
) -> CachedTranscription:
    """Cache-aware transcribe keyed by `(transcriber.backend,
    transcriber.model_name)`. On miss/force/fingerprint-mismatch, runs
    the transcriber, applies the hallucination filter, and writes a new
    entry without evicting any other entry. Returns the fresh
    `CachedTranscription`.

    Language-aware: `source_lang` (the language pin, ADR-0010)
    is forwarded to the Transcriber and part of the match key — an entry
    transcribed under one pin must not be served when the caller now
    wants another. Adapters that ignore the kwarg record an empty
    `source_language`, so the match is trivially "both empty" there.

    Prompt-aware: `initial_prompt` and `hotwords` are part of the match
    key too. A cached entry written under `initial_prompt="A"` must
    not be served when the caller now wants `initial_prompt="B"` —
    otherwise editing the session-meta override and re-running would
    silently return the stale transcript. Adapters that don't consume
    these kwargs (Voxtral / Parakeet today) record empty strings, so the
    match is trivially "both empty" there.

    Rules-aware WITHOUT re-running the model: `hallucination_rules` is
    deliberately NOT part of the match key (a rule edit must not force
    every WAV through the transcriber again). Instead a cache hit
    re-applies the filter over the entry's reconstituted raw result —
    `segments + suppressed_hallucinations`, both already persisted — and
    rewrites the sidecar when the kept/suppressed split changed. Adding a
    rule and re-running therefore actually drops the hallucination from
    the merged transcript (`session_merge` re-reads the sidecar), and
    removing one restores the segment; when nothing changed the entry is
    returned untouched, so a plain re-run stays a pure cache hit."""
    backend = transcriber.backend
    model = transcriber.model_name
    size, mtime_ns = _wav_fingerprint(wav_path)

    # Snap a multi-language candidate set to a concrete per-region language
    # BEFORE the cache check, so the chosen language flows through the existing
    # `source_lang` channel and becomes part of the match key. Resolving up front
    # (rather than only on a miss) is what makes the cache correct when the
    # operator CHANGES the meeting's languages: a different set yields a
    # different pin → the entry misses → it re-detects, instead of serving a
    # transcript chosen under the old set. A singleton set or an explicit pin
    # already arrives as `source_lang`; adapters without constrained detection
    # leave it None and auto-detect. The detect is a cheap one-window pass; the
    # cache still spares the expensive transcribe on a hit. See ADR-0010.
    if source_lang is None and candidate_languages and isinstance(transcriber, ConstrainedLanguageDetector):
        source_lang = transcriber.detect_constrained_language(wav_path, candidate_languages) or None

    if not force:
        existing = _read_entry_for(wav_path, backend=backend, model=model)
        if (
            existing is not None
            and existing.wav_size == size
            and existing.wav_mtime_ns == mtime_ns
            and (existing.result.source_language or "") == (source_lang or "")
            and (existing.result.initial_prompt_used or "") == (initial_prompt or "")
            and (existing.result.hotwords_used or "") == (hotwords or "")
        ):
            return _refilter_cached(
                wav_path,
                existing,
                rules=hallucination_rules,
                backend=backend,
                model=model,
            )

    started = datetime.now(UTC)
    raw = transcriber.transcribe(
        wav_path,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
        source_lang=source_lang,
    )
    filtered = hallucinations_mod.apply(raw, rules=hallucination_rules)
    finished = datetime.now(UTC)

    wav_start = parse_wav_start(wav_path.name)
    cached = CachedTranscription(
        result=filtered,
        transcribed_at=finished,
        transcribe_ms=int((finished - started).total_seconds() * 1000),
        source=source,
        wav_start=wav_start,
        speaker_name=parse_wav_speaker_slug(wav_path.name),
        wav_size=size,
        wav_mtime_ns=mtime_ns,
        rules_sig=_rules_fingerprint(hallucination_rules),
    )
    _write_entry(wav_path, cached, backend=backend, model=model)
    return cached


def _segment_order(segment: TranscriptionSegment) -> tuple[float, float]:
    return (segment.start, segment.end)


def _rules_fingerprint(rules: list[dict[str, Any]]) -> str:
    """Stable digest of the rule set a filter pass ran under — the sidecar's
    `rules_sig`. `raw` is the whole rule (kind is derived from its prefix), and
    a cross-process-stable hash is required because the entry outlives us."""
    joined = "\n".join(str(r.get("raw", "")) for r in rules)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _refilter_cached(
    wav_path: Path,
    existing: CachedTranscription,
    *,
    rules: list[dict[str, Any]],
    backend: str,
    model: str,
) -> CachedTranscription:
    """Re-run the hallucination filter over a CACHE HIT and persist the result
    when the kept/suppressed split changed. Returns the entry to serve.

    Gated on the entry's `rules_sig`: the filter re-runs only when the rule set
    differs from the one the stored split was decided under. Rules almost never
    change, and the pass is O(segments × rules) per WAV *per cover model* — on
    an all-cached 400-WAV meeting it was the whole cost of a "re-transcribe"
    that does nothing. A legacy entry (no sig) refilters once and is then
    stamped, WITHOUT re-pointing `_primary` (an unchanged re-run must never
    steal an operator's pin).

    The entry stores both halves of the last filter pass (`segments` and
    `suppressed_hallucinations`), so their concatenation IS the transcriber's
    raw output — no model run is needed to re-decide the split under edited
    rules. `matched_rule` is cleared on the suppressed half first so a segment
    that is kept this time doesn't carry a stale annotation.

    Concatenating kept-then-suppressed (rather than sorting) makes the
    unchanged case an EXACT round-trip — `hallucinations.apply` preserves
    relative order within each half — so an unedited rules file never triggers
    a spurious rewrite. Only when the split really changed do we sort both
    halves back into temporal order, so a segment restored by a REMOVED rule
    lands where it belongs in the per-WAV sidecar (the merged view re-sorts by
    absolute start anyway)."""
    sig = _rules_fingerprint(rules)
    if existing.rules_sig == sig:
        return existing
    raw_segments = tuple(existing.result.segments) + tuple(
        replace(seg, matched_rule=None) for seg in existing.result.suppressed_hallucinations
    )
    raw = replace(existing.result, segments=raw_segments, suppressed_hallucinations=())
    refiltered = hallucinations_mod.apply(raw, rules=rules)
    if (
        refiltered.segments == existing.result.segments
        and refiltered.suppressed_hallucinations == existing.result.suppressed_hallucinations
    ):
        # Same split under a different rule set: stamp the sig so the next hit
        # skips the pass entirely, leaving the primary pointer alone.
        stamped = replace(existing, rules_sig=sig)
        _write_entry(wav_path, stamped, backend=backend, model=model, make_primary=False)
        return stamped
    ordered = tuple(sorted(refiltered.segments, key=_segment_order))
    refiltered = replace(
        refiltered,
        segments=ordered,
        suppressed_hallucinations=tuple(sorted(refiltered.suppressed_hallucinations, key=_segment_order)),
        # Recompute `text` from the SORTED kept segments. `hallucinations.apply`
        # already recomputes it, but not usefully here: it short-circuits on an
        # empty rule set (so a rules edit that RESTORES a segment left the
        # restored phrase out of `text` entirely), and otherwise joins in
        # kept-then-restored concat order, which the sort above then reorders.
        # Either way `text` would disagree with `segments` in the persisted
        # sidecar — the exact desync `apply`'s own recompute exists to prevent.
        text=" ".join(s.text for s in ordered if s.text).strip(),
    )
    # Keep the write-time envelope (transcribed_at / transcribe_ms / wav
    # fingerprint): no model ran, only the post-decode filter was re-decided.
    refreshed = replace(existing, result=refiltered, rules_sig=sig)
    _write_entry(wav_path, refreshed, backend=backend, model=model)
    return refreshed


def _wav_fingerprint(wav_path: Path) -> tuple[int, int]:
    """Cheap content fingerprint: (size, mtime_ns). Both are zero if the
    file is missing — read_cached returns None on a missing sidecar so
    that's fine, and a fresh transcribe will overwrite the placeholder."""
    try:
        st = wav_path.stat()
        return st.st_size, st.st_mtime_ns
    except OSError:
        return 0, 0


# ---------------------------------------------------------------------------
# Per-entry I/O (private — callers go through cached_transcribe / read_cached)
# ---------------------------------------------------------------------------


def _read_entry(path: Path) -> CachedTranscription | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return _from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def _read_entry_for(wav_path: Path, *, backend: str, model: str) -> CachedTranscription | None:
    """Return the cached entry for this specific `(backend, model)`,
    or None. In the new layout reads the keyed `<key>.json` directly (the
    same key `_write_entry` / `set_primary_transcript` write under, so a
    sanitized-name collision can't cause a miss); in the legacy layout returns
    the single sidecar only if its embedded backend+model match."""
    d = transcripts_dir(wav_path)
    if d.is_dir():
        return _read_entry(d / f"{_entry_key(backend, model)}.json")
    raw_paths = _resolve_sidecar_paths(wav_path)
    if not raw_paths:
        return None
    entry = _read_entry(raw_paths[0])
    if entry is not None and entry.result.backend == backend and entry.result.model == model:
        return entry
    return None


def _primary_sidecar_path(wav_path: Path) -> Path | None:
    """The on-disk path of the primary sidecar for this WAV, or None
    if no transcript is cached. Picks the new-layout primary when the
    `<wav>.transcripts/` directory exists, otherwise the legacy
    `<wav>.json`.

    Parse-free: resolves the primary path WITHOUT reading any sidecar body,
    so `read_cached` / `read_primary_payload` / `read_primary_marker` parse
    only the one primary sidecar (never every sibling) on the /api/state hot
    path — and a valid-JSON-but-incomplete primary still streams its raw dict
    via `read_primary_payload` rather than resolving to None."""
    return _primary_path_of(wav_path, _resolve_sidecar_paths(wav_path))


def _primary_filename(transcripts_dir: Path, sidecars: list[Path] | None = None) -> str | None:
    """The filename (just the leaf, not the full path) of the primary
    sidecar inside `transcripts_dir`. Honors `_primary` when valid;
    otherwise falls back to the newest-mtime sidecar.

    `sidecars` may be supplied by a caller that already globbed the
    directory to share the syscall — important on the dashboard hot
    path."""
    pointer = transcripts_dir / _PRIMARY_POINTER
    if pointer.is_file():
        try:
            key = pointer.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
        if key:
            candidate = transcripts_dir / f"{key}.json"
            if candidate.is_file():
                return candidate.name
    if sidecars is None:
        sidecars = list(transcripts_dir.glob("*.json"))
    if not sidecars:
        return None
    # Stat defensively: `sidecars` was globbed (here or by a caller sharing the
    # syscall) and a concurrent `delete_session_audio` rmtree can remove an
    # entry between the glob and the stat — the /api/state poll walks sessions
    # on a worker thread while a delete runs on another. A bare
    # `max(..., key=p.stat)` let that FileNotFoundError escape read_cached /
    # read_primary_payload / read_primary_marker and 500 the poll for the
    # duration of the delete. Skip the vanished ones; None when none survive.
    survivors: list[tuple[int, str]] = []
    for path in sidecars:
        with contextlib.suppress(OSError):
            survivors.append((path.stat().st_mtime_ns, path.name))
    if not survivors:
        return None
    # `key=` on the mtime alone: ties keep the first-seen entry (a coarse-mtime
    # filesystem can give two sidecars the same stamp), as the running max did.
    return max(survivors, key=lambda s: s[0])[1]


def _write_entry(
    wav_path: Path,
    cached: CachedTranscription,
    *,
    backend: str,
    model: str,
    make_primary: bool = True,
) -> None:
    _migrate_legacy_if_needed(wav_path)
    d = transcripts_dir(wav_path)
    key = _entry_key(backend, model)
    atomic_write_text(
        d / f"{key}.json",
        json.dumps(_to_dict(cached), indent=2, ensure_ascii=False),
    )
    # A fresh write becomes the primary — operators flipping models on
    # the same WAV expect the dashboard to show the just-produced result
    # unless they explicitly pinned a different primary. `make_primary=False`
    # is for a bookkeeping-only rewrite (the `rules_sig` stamp), which must not
    # move the pointer.
    if make_primary:
        atomic_write_text(d / _PRIMARY_POINTER, key)


def _migrate_legacy_if_needed(wav_path: Path) -> None:
    """Move a legacy `<wav>.json` into the new-layout directory under
    its own `(backend, model)` key, so the two formats never coexist
    for the same WAV. No-op if the directory already exists or no
    legacy file is present. Unparseable legacy files are removed so
    they can't shadow the new layout on subsequent reads."""
    d = transcripts_dir(wav_path)
    if d.exists():
        return
    legacy = legacy_sidecar(wav_path)
    if not legacy.is_file():
        return
    parsed = _read_entry(legacy)
    if parsed is None:
        # Best-effort cleanup of a corrupt legacy sidecar. If unlink fails
        # (Windows file lock, perms) the file stays put — subsequent reads
        # will keep returning None from `_read_entry`, which is the same
        # behavior as before this PR, so the failure is non-fatal.
        with contextlib.suppress(OSError):
            legacy.unlink()
        return
    d.mkdir(parents=True, exist_ok=True)
    key = _entry_key(parsed.result.backend, parsed.result.model)
    target = d / f"{key}.json"
    try:
        legacy.replace(target)
    except OSError:
        try:
            atomic_write_text(target, legacy.read_text(encoding="utf-8"))
            legacy.unlink()
        except OSError:
            # Neither the move nor the copy landed (Windows sharing violation
            # on the legacy file, read-only FS, disk full). Swallowing is
            # correct — a failed migration must not fail the transcribe that
            # triggered it — but the empty `<wav>.transcripts/` we just made
            # would PERMANENTLY hide the transcript: `_resolve_sidecar_paths`
            # takes the `d.is_dir()` branch, globs nothing, and the migration
            # never retries because it early-returns on `if d.exists()`. So
            # remove the directory again (rmdir only succeeds while it is
            # empty, which it is — atomic_write_text cleans up its own
            # tempfile) and leave the legacy layout intact: reads keep serving
            # `<wav>.json`. What's lost is only this attempt — if the write
            # that triggered the migration goes on to create the directory for
            # its OWN entry, the legacy one is shadowed until it is
            # re-transcribed, but the operator still sees a transcript instead
            # of the blank row an empty directory produced.
            with contextlib.suppress(OSError):
                d.rmdir()
            return
    atomic_write_text(d / _PRIMARY_POINTER, key)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _to_dict(cached: CachedTranscription) -> dict[str, Any]:
    r = cached.result
    out: dict[str, Any] = {
        "transcriber": r.transcriber,
        "backend": r.backend,
        "device": r.device,
        "model": r.model,
        "language": r.language,
        "language_probability": r.language_probability,
        "duration": r.duration,
        "segments": [s.to_mapping() for s in r.segments],
        "text": r.text,
        "initial_prompt_used": r.initial_prompt_used,
        "hotwords_used": r.hotwords_used,
        "source_language": r.source_language,
        "quality_settings": r.quality_settings,
        "suppressed_hallucinations": [s.to_mapping() for s in r.suppressed_hallucinations],
        "transcribed_at": cached.transcribed_at.isoformat(),
        "transcribe_ms": cached.transcribe_ms,
        "source": cached.source,
        "speaker_name": cached.speaker_name,
        "wav_size": cached.wav_size,
        "wav_mtime_ns": cached.wav_mtime_ns,
    }
    if cached.wav_start is not None:
        out["wav_start"] = cached.wav_start.isoformat()
    if cached.rules_sig is not None:
        out["rules_sig"] = cached.rules_sig
    return out


def _from_dict(data: dict[str, Any]) -> CachedTranscription:
    result = TranscriptionResult(
        transcriber=data["transcriber"],
        # backend was added later — legacy sidecars without it load with "".
        backend=data.get("backend", ""),
        device=data["device"],
        model=data["model"],
        language=data.get("language", "?"),
        language_probability=float(data.get("language_probability", 0.0) or 0.0),
        duration=float(data.get("duration", 0.0) or 0.0),
        text=data.get("text", ""),
        segments=tuple(TranscriptionSegment.from_payload(s) for s in data.get("segments", [])),
        initial_prompt_used=data.get("initial_prompt_used", ""),
        hotwords_used=data.get("hotwords_used", ""),
        quality_settings=data.get("quality_settings", {}) or {},
        suppressed_hallucinations=tuple(
            TranscriptionSegment.from_payload(s) for s in data.get("suppressed_hallucinations", [])
        ),
        # source_language landed later than the rest of the schema — legacy
        # sidecars without it load with the empty-string default. (Sidecars
        # may also carry a Canary-era "target_language"; it's ignored — the
        # accepted trade-off is that a Canary-translated sidecar's text now
        # renders with no translation cue. See ADR-0006's update note.)
        source_language=data.get("source_language", "") or "",
    )
    transcribed_at = parse_iso(data["transcribed_at"])
    if transcribed_at is None:
        raise ValueError("transcribed_at missing")
    return CachedTranscription(
        result=result,
        transcribed_at=transcribed_at,
        transcribe_ms=int(data.get("transcribe_ms", 0)),
        source=data.get("source", "original"),
        wav_start=parse_iso(data.get("wav_start")),
        speaker_name=data.get("speaker_name", ""),
        # Older sidecars don't carry the fingerprint; default to 0 so the
        # next `cached_transcribe` call sees a mismatch against the live
        # WAV stat and re-runs. That's a one-time cost; subsequent calls
        # hit the cache normally.
        wav_size=int(data.get("wav_size", 0) or 0),
        wav_mtime_ns=int(data.get("wav_mtime_ns", 0) or 0),
        # Absent on a pre-`rules_sig` sidecar → None, which never equals a
        # fingerprint, so the entry refilters once and is stamped.
        rules_sig=data.get("rules_sig") or None,
    )
