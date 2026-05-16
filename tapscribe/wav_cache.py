"""Per-WAV transcription cache.

Every transcribed WAV gets a sidecar `<name>.json` next to the WAV.
`cached_transcribe` is the policy-aware entry point: cache hit returns
the parsed sidecar, miss runs the Transcriber + hallucination filter and
writes the result back. `read_cached` is the pure read.

The on-disk format is a flat JSON object whose keys span both the
TranscriptionResult fields and the write-time envelope (when this WAV
was transcribed, which source folder, the speaker slug parsed from the
filename, the absolute UTC start time). Land 2's `merge_session` reads
the same sidecars to build the session-level transcript.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
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
    must be more than just model name."""

    result: TranscriptionResult
    transcribed_at: datetime
    transcribe_ms: int
    source: str
    wav_start: datetime | None
    speaker_name: str
    wav_size: int = 0
    wav_mtime_ns: int = 0


def read_cached(wav_path: Path) -> CachedTranscription | None:
    """Return the parsed sidecar for `wav_path`, or None if the file is
    missing or unparseable."""
    sidecar = wav_path.with_suffix(".json")
    if not sidecar.is_file():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return _from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def cached_transcribe(
    wav_path: Path,
    transcriber: Transcriber,
    *,
    initial_prompt: str | None,
    hotwords: str | None,
    hallucination_rules: list[dict[str, Any]],
    force: bool = False,
    source: str = "original",
) -> CachedTranscription:
    """Try the cache; on miss/force/model-mismatch, transcribe + apply +
    write sidecar. Returns the `CachedTranscription`."""
    size, mtime_ns = _wav_fingerprint(wav_path)
    if not force:
        cached = read_cached(wav_path)
        if (
            cached is not None
            and cached.result.model == transcriber.model_name
            and cached.wav_size == size
            and cached.wav_mtime_ns == mtime_ns
        ):
            return cached

    started = datetime.now(timezone.utc)
    raw = transcriber.transcribe(wav_path, initial_prompt=initial_prompt, hotwords=hotwords)
    filtered = hallucinations_mod.apply(raw, rules=hallucination_rules)
    finished = datetime.now(timezone.utc)

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
    _write_sidecar(wav_path, cached)
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
# Serialization (kept private so callers go through cached_transcribe / read_cached)
# ---------------------------------------------------------------------------


def _write_sidecar(wav_path: Path, cached: CachedTranscription) -> None:
    sidecar = wav_path.with_suffix(".json")
    sidecar.write_text(
        json.dumps(_to_dict(cached), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _to_dict(cached: CachedTranscription) -> dict[str, Any]:
    r = cached.result
    out: dict[str, Any] = {
        "transcriber": r.transcriber,
        "device": r.device,
        "model": r.model,
        "language": r.language,
        "language_probability": r.language_probability,
        "duration": r.duration,
        "segments": [s.to_mapping() for s in r.segments],
        "text": r.text,
        "initial_prompt_used": r.initial_prompt_used,
        "hotwords_used": r.hotwords_used,
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
