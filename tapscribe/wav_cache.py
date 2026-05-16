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
    write-time envelope (when, source folder, parsed speaker, wav_start)."""
    result: TranscriptionResult
    transcribed_at: datetime
    transcribe_ms: int
    source: str
    wav_start: datetime | None
    speaker_name: str


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
    if not force:
        cached = read_cached(wav_path)
        if cached is not None and cached.result.model == transcriber.model_name:
            return cached

    started = datetime.now(timezone.utc)
    raw = transcriber.transcribe(wav_path, initial_prompt=initial_prompt, hotwords=hotwords)
    filtered = hallucinations_mod.apply(raw, rules=hallucination_rules)
    finished = datetime.now(timezone.utc)

    wav_start = parse_wav_start(wav_path.name)
    cached = CachedTranscription(
        result=filtered,
        transcribed_at=finished,
        transcribe_ms=int((finished - started).total_seconds() * 1000),
        source=source,
        wav_start=wav_start,
        speaker_name=parse_wav_speaker_slug(wav_path.name),
    )
    _write_sidecar(wav_path, cached)
    return cached


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
    )
