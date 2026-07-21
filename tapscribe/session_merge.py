"""Session merge — selection + merge of per-WAV results into a session transcript.

The module is split into three responsibilities:

  - `select_session_wavs` — pure filtering of a session directory by time
    range, file health, and silence floor. Returns a `SessionSelection`.
  - `merge_session` — pure construction of a `SessionTranscript` from the
    per-WAV JSON sidecars next to the selected WAVs. Doesn't transcribe
    anything; WAVs without a sidecar are recorded in
    `skipped_no_cache` and skipped.
  - The actual transcribing of missing WAVs lives in `tapscribe.wav_cache`
    (`cached_transcribe`). The route handler orchestrates the three.

This split makes both phases independently testable and lets a future
"re-merge using only cached results" workflow exist without going
through a Transcriber.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config
from .audio import wav_duration_s, wav_rms_dbfs
from .session_paths import DIRNAME_STRIPPED, FILENAME_STRIP_META_JSON, SessionPathError, _safe_part
from .sessions import strip_meta_owner_by_clip, valid_strip_meta
from .text import parse_iso, parse_wav_start
from .wav_cache import read_cached

# ---------------------------------------------------------------------------
# Selection verdicts — raised when a session/range yields nothing usable.
# They live here, next to `select_session_wavs`, because they are *selection*
# outcomes, not transcription ones: both Batch transcription and Batch strip
# raise `NoUsableWavs`, so neither orchestrator should own it. The route layer
# maps NoUsableWavs → 404 and InvalidRange → 400.
# ---------------------------------------------------------------------------


class NoUsableWavs(Exception):
    """The session/range filter rejected every WAV — the directory is empty or
    the from_iso/to_iso range matched nothing. "Valid inputs, empty result"
    (vs. `InvalidRange`, which is "inputs unparseable")."""


class InvalidRange(Exception):
    """`select_session_wavs` got an unparseable `from_iso` / `to_iso`. The
    caller's inputs were syntactically wrong (vs. `NoUsableWavs`'s valid-but-
    empty)."""


@dataclass(frozen=True)
class SessionSelection:
    """Result of `select_session_wavs`. Carries the selected WAV paths
    plus the names that were filtered out, so the merger / caller can
    surface them to the operator."""

    session_dir: Path
    source: str  # "original" | "stripped"
    wavs: tuple[Path, ...]
    skipped_bad: tuple[str, ...]
    skipped_silent: tuple[str, ...]
    from_iso: str | None = None
    to_iso: str | None = None


def select_session_wavs(
    session_dir: Path,
    *,
    from_iso: str | None = None,
    to_iso: str | None = None,
    source: str = "original",
) -> SessionSelection:
    """Return the set of WAVs in `session_dir` that should participate in
    a merge. Pure: reads only WAV headers + filesystem metadata; never
    invokes a Transcriber.

    Filters applied:
      - `source="stripped"` reads from `session_dir/stripped/` instead.
        If that subdirectory doesn't exist, returns an empty selection
        (caller can decide whether to fall back).
      - Files smaller than 64 bytes or with unreadable durations are
        recorded in `skipped_bad`.
      - The ORIGINAL of each WAV (always read from `session_dir/`,
        even when `source="stripped"`) must have RMS above
        `SILENT_RMS_DBFS_FLOOR`. Otherwise the WAV is recorded in
        `skipped_silent` — Whisper hallucinates on near-silent audio.
      - `from_iso` / `to_iso` filter by the timestamp embedded in the
        filename. Raises `ValueError` if either is unparseable. Applied
        BEFORE the health/silence gates above, so a narrow range over a
        long meeting opens only the WAVs it asked for, and `skipped_bad` /
        `skipped_silent` describe only in-range files.
    """
    base_dir = session_dir
    owner_by_clip: dict[str, str] = {}
    if source == "stripped":
        wav_dir = session_dir / DIRNAME_STRIPPED
        if not wav_dir.is_dir():
            return SessionSelection(
                session_dir=session_dir,
                source=source,
                wavs=(),
                skipped_bad=(),
                skipped_silent=(),
                from_iso=from_iso,
                to_iso=to_iso,
            )
        # Build owner_by_clip from strip-meta.json, so the silence gate below
        # can key off the true original instead of the stripped clip itself.
        # Read directly (not via `sessions.read_strip_meta`): this function is
        # pure and makes no assumption that `session_dir` lives under
        # `config.RECORDINGS_DIR` — every selection test (including this
        # issue's) builds `session_dir` from a bare tmp_path, and the two
        # production callers (`batch_transcribe`, `batch_pipeline`) only ever
        # reach here with a `session_dir` already validated by
        # `resolve_session_dir`, so the containment guarantee holds there too,
        # just one layer up rather than re-checked here. `valid_strip_meta`
        # still enforces the one shape contract shared with the safe reader.
        meta_path = wav_dir / FILENAME_STRIP_META_JSON
        try:
            with meta_path.open("r", encoding="utf-8") as fh:
                meta = valid_strip_meta(json.load(fh))
        except (OSError, ValueError):
            meta = None
        if meta is not None:
            owner_by_clip = strip_meta_owner_by_clip(meta)
    elif source in (None, "", "original"):
        wav_dir = session_dir
    else:
        raise ValueError(f"unknown source: {source!r}")

    from_dt = parse_iso(from_iso)
    to_dt = parse_iso(to_iso)

    selected: list[Path] = []
    skipped_bad: list[str] = []
    skipped_silent: list[str] = []

    for wav in sorted(wav_dir.glob("*.wav")):
        wav_start = parse_wav_start(wav.name)
        if wav_start is None:
            # Without a parseable timestamp the WAV can't participate in
            # a chronological merge; treat as "bad" rather than silent.
            skipped_bad.append(wav.name)
            continue

        # Range filter FIRST — it is the only filename-only gate, and every
        # gate below opens the file (`wav_duration_s` reads the header,
        # `wav_rms_dbfs` reads every frame). Filtering afterwards made a 3-WAV
        # range against a 400-WAV meeting read all 400 end-to-end before
        # discarding 397, and let out-of-range files land in
        # `skipped_bad`/`skipped_silent`, whose counts are persisted into
        # `session-transcript.json` describing files the caller never asked for.
        if from_dt and wav_start < from_dt:
            continue
        if to_dt and wav_start > to_dt:
            continue

        try:
            size = wav.stat().st_size
        except OSError:
            size = 0
        if size < 64 or wav_duration_s(wav) <= 0.0:
            skipped_bad.append(wav.name)
            continue

        # Silence gate always reads the ORIGINAL even when source=stripped;
        # the stripped sibling's RMS can be misleadingly high because
        # silero may have false-positive'd on a brief noise burst.
        original_name = owner_by_clip.get(wav.name, wav.name)
        try:
            # `original_name` came from strip-meta.json content, not a
            # filesystem listing — run it through the same sanitiser
            # `session_paths` uses for any name headed into a path join, so a
            # malformed/adversarial sidecar entry can't walk `original_path`
            # outside `base_dir`. An invalid name just isn't a usable owner.
            _safe_part(original_name, "clip owner")
        except SessionPathError:
            original_name = wav.name
        original_path = base_dir / original_name
        if not original_path.is_file():
            # The original wasn't in session_dir/ — fall back to checking the
            # path we're actually using.
            original_path = wav
        if wav_rms_dbfs(original_path) < config.SILENT_RMS_DBFS_FLOOR:
            skipped_silent.append(wav.name)
            continue

        selected.append(wav)

    return SessionSelection(
        session_dir=session_dir,
        source=source,
        wavs=tuple(selected),
        skipped_bad=tuple(skipped_bad),
        skipped_silent=tuple(skipped_silent),
        from_iso=from_iso,
        to_iso=to_iso,
    )


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSegment:
    """One segment in the merged session timeline. `abs_start` /
    `abs_end` are tz-aware datetimes — `wav_start + segment_offset`."""

    abs_start: datetime
    abs_end: datetime
    speaker: str
    text: str
    source_wav: str
    avg_logprob: float | None = None
    low_confidence: bool = False


@dataclass(frozen=True)
class SuppressedSessionSegment:
    """A hallucination-filtered segment surfaced at session level for the
    dashboard's audit table."""

    abs_start: datetime
    speaker: str
    text: str
    matched_rule: str
    source_wav: str


@dataclass(frozen=True)
class SessionTranscript:
    """The merged transcript for one session.

    Wire shape (via `to_dict()`):
      - `speaking_seconds` is a dict keyed by speaker (replaces the
        prior parallel arrays `speakers[]` + `speaking_seconds[]`).
      - `abs_hms` is dropped from segments — consumers format from
        `abs_start` (ISO string).
    """

    session: str
    model: str
    transcriber: str
    backend: str
    device: str
    source: str
    from_iso: str | None
    to_iso: str | None
    transcribed_at: datetime
    transcribe_ms: int
    wav_count: int
    skipped_bad_count: int
    skipped_silent_count: int
    skipped_no_cache: tuple[str, ...]
    speakers: tuple[str, ...]
    speaking_seconds: dict[str, float]
    segments: tuple[SessionSegment, ...]
    suppressed: tuple[SuppressedSessionSegment, ...]
    plain_text: str
    low_confidence_count: int
    # The first non-empty per-WAV `source_language` (the language the models
    # were told to expect) — a session-level display hint, not per-segment
    # truth; the per-WAV sidecars carry the real values.
    source_language: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "model": self.model,
            "transcriber": self.transcriber,
            "backend": self.backend,
            "device": self.device,
            "source": self.source,
            "from_iso": self.from_iso,
            "to_iso": self.to_iso,
            "transcribed_at": self.transcribed_at.isoformat(),
            "transcribe_ms": self.transcribe_ms,
            "wav_count": self.wav_count,
            "skipped_bad_count": self.skipped_bad_count,
            "skipped_silent_count": self.skipped_silent_count,
            "skipped_no_cache": list(self.skipped_no_cache),
            "speakers": list(self.speakers),
            "speaking_seconds": dict(self.speaking_seconds),
            "segments": [_segment_to_dict(s) for s in self.segments],
            "suppressed": [_suppressed_to_dict(s) for s in self.suppressed],
            "suppressed_count": len(self.suppressed),
            "plain_text": self.plain_text,
            "low_confidence_count": self.low_confidence_count,
            "source_language": self.source_language,
        }


_LOW_CONFIDENCE_LOGPROB_THRESHOLD = -0.5


def merge_session(selection: SessionSelection) -> SessionTranscript:
    """Read each WAV's cached sidecar from `selection.wavs` and build a
    merged session transcript. WAVs without a cached sidecar are recorded
    in `skipped_no_cache` and contribute no segments.

    Pure: no Transcriber, no filesystem writes — only sidecar reads.
    Callers (the route handler) are responsible for running
    `cached_transcribe` on each selected WAV beforehand if they want
    a full merge.
    """
    started = datetime.now(UTC)

    segments: list[SessionSegment] = []
    suppressed: list[SuppressedSessionSegment] = []
    skipped_no_cache: list[str] = []

    # Track which Transcriber + model produced each WAV's result. They
    # *should* be uniform across a session; we surface the first non-empty
    # values on the merged transcript.
    transcriber_name = ""
    backend_label = ""
    device_label = ""
    model_label = ""
    # First non-empty source_language seen across the session's sidecars
    # wins. For a session with mixed source langs this is an
    # oversimplification, but the per-WAV JSON still carries the real
    # value — the session-level field is just a display hint.
    source_language_label = ""

    for wav in selection.wavs:
        cached = read_cached(wav)
        if cached is None:
            skipped_no_cache.append(wav.name)
            continue
        if not transcriber_name:
            transcriber_name = cached.result.transcriber
            backend_label = cached.result.backend
            device_label = cached.result.device
            model_label = cached.result.model
        if not source_language_label and cached.result.source_language:
            source_language_label = cached.result.source_language

        wav_start = cached.wav_start or datetime.fromtimestamp(wav.stat().st_mtime, tz=UTC)
        speaker = cached.speaker_name or "<anon>"

        for seg in cached.result.segments:
            abs_start = wav_start + timedelta(seconds=seg.start)
            abs_end = wav_start + timedelta(seconds=seg.end)
            low_conf = seg.avg_logprob is not None and seg.avg_logprob < _LOW_CONFIDENCE_LOGPROB_THRESHOLD
            segments.append(
                SessionSegment(
                    abs_start=abs_start,
                    abs_end=abs_end,
                    speaker=speaker,
                    text=seg.text,
                    source_wav=wav.name,
                    avg_logprob=seg.avg_logprob,
                    low_confidence=low_conf,
                )
            )
        for sup in cached.result.suppressed_hallucinations:
            abs_start = wav_start + timedelta(seconds=sup.start)
            suppressed.append(
                SuppressedSessionSegment(
                    abs_start=abs_start,
                    speaker=speaker,
                    text=sup.text,
                    matched_rule=sup.matched_rule or "",
                    source_wav=wav.name,
                )
            )

    segments.sort(key=lambda s: s.abs_start)
    suppressed.sort(key=lambda s: s.abs_start)

    speakers_set = sorted({s.speaker for s in segments if s.speaker})
    speaking_seconds: dict[str, float] = {sp: 0.0 for sp in speakers_set}
    for s in segments:
        if s.speaker in speaking_seconds:
            speaking_seconds[s.speaker] += max(0.0, (s.abs_end - s.abs_start).total_seconds())
    speaking_seconds = {k: round(v, 2) for k, v in speaking_seconds.items()}

    plain_lines: list[str] = []
    for s in segments:
        if not s.text:
            continue
        line = f"[{s.abs_start.strftime('%H:%M:%S')}] {s.speaker}: {s.text}"
        if s.low_confidence:
            line += " [uncertain]"
        plain_lines.append(line)
    plain_text = "\n".join(plain_lines)
    low_confidence_count = sum(1 for s in segments if s.low_confidence)

    finished = datetime.now(UTC)
    return SessionTranscript(
        session=selection.session_dir.name,
        model=model_label,
        transcriber=transcriber_name,
        backend=backend_label,
        device=device_label,
        source=selection.source,
        from_iso=selection.from_iso,
        to_iso=selection.to_iso,
        transcribed_at=finished,
        transcribe_ms=int((finished - started).total_seconds() * 1000),
        wav_count=len(selection.wavs) - len(skipped_no_cache),
        skipped_bad_count=len(selection.skipped_bad),
        skipped_silent_count=len(selection.skipped_silent),
        skipped_no_cache=tuple(skipped_no_cache),
        speakers=tuple(speakers_set),
        speaking_seconds=speaking_seconds,
        segments=tuple(segments),
        suppressed=tuple(suppressed),
        plain_text=plain_text,
        low_confidence_count=low_confidence_count,
        source_language=source_language_label,
    )


def _segment_to_dict(seg: SessionSegment) -> dict[str, Any]:
    out: dict[str, Any] = {
        "abs_start": seg.abs_start.isoformat(),
        "abs_end": seg.abs_end.isoformat(),
        "speaker": seg.speaker,
        "text": seg.text,
        "source_wav": seg.source_wav,
        "low_confidence": seg.low_confidence,
    }
    if seg.avg_logprob is not None:
        out["avg_logprob"] = seg.avg_logprob
    return out


def _suppressed_to_dict(sup: SuppressedSessionSegment) -> dict[str, Any]:
    return {
        "abs_start": sup.abs_start.isoformat(),
        "speaker": sup.speaker,
        "text": sup.text,
        "matched_rule": sup.matched_rule,
        "source_wav": sup.source_wav,
    }
