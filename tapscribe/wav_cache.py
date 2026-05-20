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
time) and the on-disk WAV fingerprint (size + mtime). See
CONTEXT.md "Per-WAV transcript cache" for the layout.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import hallucinations as hallucinations_mod
from .text import parse_iso, parse_wav_speaker_slug, parse_wav_start
from .transcribers.base import (
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


def _transcripts_dir(wav_path: Path) -> Path:
    return wav_path.with_suffix(".transcripts")


def _legacy_sidecar(wav_path: Path) -> Path:
    return wav_path.with_suffix(".json")


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


def read_all_cached(wav_path: Path) -> list[CachedTranscription]:
    """Every cached transcript for `wav_path`, one per (backend, model).
    Unparseable sidecars are silently dropped. Order is unspecified."""
    d = _transcripts_dir(wav_path)
    if d.is_dir():
        out: list[CachedTranscription] = []
        for entry in sorted(d.glob("*.json")):
            cached = _read_entry(entry)
            if cached is not None:
                out.append(cached)
        return out
    legacy = _read_entry(_legacy_sidecar(wav_path))
    return [legacy] if legacy is not None else []


def cache_listing(wav_path: Path) -> list[dict[str, Any]]:
    """Compact per-(backend, model) listing for dashboards. One walk,
    one parse per entry: returns `{"backend", "model", "is_primary",
    "transcribe_ms"?}` dicts ready for the wire. Single-sidecar legacy
    WAVs return a one-element list with `is_primary=True`."""
    d = _transcripts_dir(wav_path)
    if d.is_dir():
        sidecars = sorted(d.glob("*.json"))
        if not sidecars:
            return []
        primary = _primary_filename(d, sidecars)
        out: list[dict[str, Any]] = []
        for sidecar in sidecars:
            entry = _read_entry(sidecar)
            if entry is None:
                continue
            item: dict[str, Any] = {
                "backend": entry.result.backend,
                "model": entry.result.model,
                "is_primary": sidecar.name == primary,
            }
            if entry.transcribe_ms:
                item["transcribe_ms"] = entry.transcribe_ms
            out.append(item)
        return out
    legacy = _read_entry(_legacy_sidecar(wav_path))
    if legacy is None:
        return []
    item: dict[str, Any] = {
        "backend": legacy.result.backend,
        "model": legacy.result.model,
        "is_primary": True,
    }
    if legacy.transcribe_ms:
        item["transcribe_ms"] = legacy.transcribe_ms
    return [item]


def set_primary_transcript(wav_path: Path, *, backend: str, model: str) -> None:
    """Point the primary at the named `(backend, model)` entry. Raises
    `FileNotFoundError` if that entry isn't cached for this WAV.

    Implicitly migrates the legacy sidecar layout into the new one if
    necessary so the pointer has somewhere to live."""
    _migrate_legacy_if_needed(wav_path)
    d = _transcripts_dir(wav_path)
    key = _entry_key(backend, model)
    target = d / f"{key}.json"
    if not target.is_file():
        raise FileNotFoundError(f"no cached transcript for backend={backend!r}, model={model!r} at {target}")
    d.mkdir(parents=True, exist_ok=True)
    (d / _PRIMARY_POINTER).write_text(key, encoding="utf-8")


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
    target_lang: str | None = None,
    force: bool = False,
    source: str = "original",
) -> CachedTranscription:
    """Cache-aware transcribe keyed by `(transcriber.backend,
    transcriber.model_name)`. On miss/force/fingerprint-mismatch, runs
    the transcriber, applies the hallucination filter, and writes a new
    entry without evicting any other entry. Returns the fresh
    `CachedTranscription`.

    Translation-aware: `source_lang` / `target_lang` are forwarded to
    the Transcriber. For Canary, a cached entry produced for
    source=en/target=es must not be served when the caller now wants
    target=fr — so the cache hit also requires matching language pair.
    Whisper / Voxtral / Parakeet ignore these kwargs, and their cached
    entries have empty source/target_language, so the match is trivially
    "both empty" for those backends.

    Prompt-aware: `initial_prompt` and `hotwords` are part of the match
    key too. A cached entry written under `initial_prompt="A"` must
    not be served when the caller now wants `initial_prompt="B"` —
    otherwise editing the session-meta override and re-running would
    silently return the stale transcript. Adapters that don't consume
    these kwargs (Voxtral / Parakeet / Canary today) record empty
    strings, so the match is trivially "both empty" there."""
    backend = transcriber.backend
    model = transcriber.model_name
    size, mtime_ns = _wav_fingerprint(wav_path)
    if not force:
        existing = _read_entry_for(wav_path, backend=backend, model=model)
        if (
            existing is not None
            and existing.wav_size == size
            and existing.wav_mtime_ns == mtime_ns
            and (existing.result.source_language or "") == (source_lang or "")
            and (existing.result.target_language or "") == (target_lang or "")
            and (existing.result.initial_prompt_used or "") == (initial_prompt or "")
            and (existing.result.hotwords_used or "") == (hotwords or "")
        ):
            return existing

    started = datetime.now(UTC)
    raw = transcriber.transcribe(
        wav_path,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
        source_lang=source_lang,
        target_lang=target_lang,
    )
    filtered = hallucinations_mod.apply(raw, rules=hallucination_rules)
    finished = datetime.now(UTC)

    wav_start = parse_wav_start(wav_path.name)
    # Re-stat after transcribe in case the WAV was being written when we
    # entered (the resume path closes the writer before transcribe runs,
    # but a future caller might not). Either way the sidecar reflects
    # what the transcriber actually saw.
    size, mtime_ns = _wav_fingerprint(wav_path)
    cached = CachedTranscription(
        result=filtered,
        transcribed_at=finished,
        transcribe_ms=int((finished - started).total_seconds() * 1000),
        source=source,
        wav_start=wav_start,
        speaker_name=parse_wav_speaker_slug(wav_path.name),
        wav_size=size,
        wav_mtime_ns=mtime_ns,
    )
    _write_entry(wav_path, cached, backend=backend, model=model)
    return cached


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
    or None. Looks first in the new-layout directory; falls back to the
    legacy `<wav>.json` only if its embedded backend+model match."""
    d = _transcripts_dir(wav_path)
    if d.is_dir():
        return _read_entry(d / f"{_entry_key(backend, model)}.json")
    legacy = _read_entry(_legacy_sidecar(wav_path))
    if legacy is None:
        return None
    if legacy.result.backend == backend and legacy.result.model == model:
        return legacy
    return None


def _primary_sidecar_path(wav_path: Path) -> Path | None:
    """The on-disk path of the primary sidecar for this WAV, or None
    if no transcript is cached. Picks the new-layout primary when the
    `<wav>.transcripts/` directory exists, otherwise the legacy
    `<wav>.json`."""
    d = _transcripts_dir(wav_path)
    if d.is_dir():
        name = _primary_filename(d)
        return d / name if name else None
    legacy = _legacy_sidecar(wav_path)
    return legacy if legacy.is_file() else None


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
    newest = max(sidecars, key=lambda p: p.stat().st_mtime_ns)
    return newest.name


def _write_entry(
    wav_path: Path,
    cached: CachedTranscription,
    *,
    backend: str,
    model: str,
) -> None:
    _migrate_legacy_if_needed(wav_path)
    d = _transcripts_dir(wav_path)
    d.mkdir(parents=True, exist_ok=True)
    key = _entry_key(backend, model)
    (d / f"{key}.json").write_text(
        json.dumps(_to_dict(cached), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # A fresh write becomes the primary — operators flipping models on
    # the same WAV expect the dashboard to show the just-produced result
    # unless they explicitly pinned a different primary.
    (d / _PRIMARY_POINTER).write_text(key, encoding="utf-8")


def _migrate_legacy_if_needed(wav_path: Path) -> None:
    """Move a legacy `<wav>.json` into the new-layout directory under
    its own `(backend, model)` key, so the two formats never coexist
    for the same WAV. No-op if the directory already exists or no
    legacy file is present. Unparseable legacy files are removed so
    they can't shadow the new layout on subsequent reads."""
    d = _transcripts_dir(wav_path)
    if d.exists():
        return
    legacy = _legacy_sidecar(wav_path)
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
            target.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
            legacy.unlink()
        except OSError:
            return
    (d / _PRIMARY_POINTER).write_text(key, encoding="utf-8")


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
        "target_language": r.target_language,
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
        # source/target_language land later than the rest of the schema — legacy
        # sidecars without them load with the empty-string default.
        source_language=data.get("source_language", "") or "",
        target_language=data.get("target_language", "") or "",
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
    )
