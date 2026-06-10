"""Batch strip-silence — drive the splitter across every WAV in a session.

The strip-silence sibling of `batch_transcribe`: same orchestrator shape
(bracket the session's job slot via `recorder.jobs.run`, loop the WAVs on a
worker thread, aggregate), same FastAPI-free contract — domain errors out, the
route maps them to HTTP codes. `SessionBusy` comes from `tapscribe.recorder`
(a JobTracker concept, raised by `run`) and `NoUsableWavs` from
`tapscribe.session_merge` (a selection verdict) — neither is transcription-
specific, so neither lives in `batch_transcribe` any more.

`strip_one_wav` — the per-WAV splitter the loop drives — lives here too: it's
the unit of work this orchestrator owns, not session bookkeeping, so it moved
out of `sessions.py` when that module was narrowed to the dashboard read path.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config
from . import strip_silence as _ss
from .audio import wav_rms_dbfs
from .recorder import Recorder
from .session_merge import NoUsableWavs
from .session_paths import resolve_session_dir, stripped_dir
from .strip_silence import SPEECH_RMS_DBFS_FLOOR
from .text import atomic_write_text, build_recorder_wav_name, parse_wav_speaker_ident, parse_wav_start


class BatchStripError(Exception):
    """Base class for strip-silence orchestration errors. Distinct from
    `BatchTranscribeError`: a strip failure isn't a transcription failure —
    they only ever shared a base by accident of where the code first grew."""


class StrippedDirUnclearable(BatchStripError):
    """`<session>/stripped/` exists but couldn't be removed before the
    re-strip — typically a file lock (Windows) or permissions. Raised
    before any WAV is touched, so nothing was modified. Routes map this
    to a 500."""


@dataclass(frozen=True)
class StripSessionRequest:
    """Inputs for stripping a session. The knob defaults live HERE (the
    dashboard's strip-silence sliders mirror them); the route forwards
    only explicitly-provided values, so `StripSessionRequest(session=…)`
    is the canonical default invocation."""

    session: str
    min_silence_ms: int = 500
    pad_ms: int = 200
    speech_floor_db: float = SPEECH_RMS_DBFS_FLOOR


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

    Used by `strip_session` below, which runs this in a worker thread.
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
    speaker_slug, ident_slug = parse_wav_speaker_ident(src.name)

    speech_samples = 0
    regions_written: list[str] = []
    region_spans: list[dict[str, float]] = []
    for start_sample, end_sample in regions:
        region_samples = samples[start_sample:end_sample]
        offset_s = start_sample / _ss.SAMPLE_RATE
        region_start = origin + timedelta(seconds=offset_s)
        fname = build_recorder_wav_name(region_start, speaker_slug, ident_slug)
        _ss.write_wav_int16(out_dir / fname, region_samples)
        regions_written.append(fname)
        region_spans.append(
            {
                "start_s": round(start_sample / _ss.SAMPLE_RATE, 3),
                "end_s": round(end_sample / _ss.SAMPLE_RATE, 3),
            }
        )
        speech_samples += len(region_samples)

    return {
        "name": src.name,
        "in_seconds": round(in_secs, 2),
        "speech_seconds": round(speech_samples / _ss.SAMPLE_RATE, 2),
        "segments": len(regions),
        "segments_filtered_below_floor": pre_filter_count - len(regions),
        "written": True,
        "regions_written": regions_written,
        "region_spans": region_spans,
        "detector": "silero-vad",
    }


async def strip_session(recorder: Recorder, req: StripSessionRequest) -> dict[str, Any]:
    """Non-destructively strip silence from every WAV in the session:
    cleaned copies land in `<session>/stripped/` (originals untouched),
    replacing any previous stripped output.

    Claims the session's `JobTracker` slot — raises `SessionBusy` when a
    transcribe/strip is already in flight, `NoUsableWavs` when the
    session has no originals. Per-WAV splitter failures don't abort the
    loop; they land in the result's `files` list as
    `{"written": False, "error": …}` rows so one corrupt WAV can't sink
    the rest of the session."""
    session_dir = resolve_session_dir(req.session)
    originals = sorted(session_dir.glob("*.wav"))
    if not originals:
        raise NoUsableWavs("no WAVs in this session to strip")

    # recorder.jobs.run brackets the "one job per session" rule: claim on
    # entry (SessionBusy if busy, releasing nothing), release on every exit.
    async with recorder.jobs.run(req.session, kind="strip", total=len(originals), status="stripping"):
        out_dir = stripped_dir(req.session)
        if out_dir.exists():
            try:
                shutil.rmtree(out_dir)
            except OSError as e:
                raise StrippedDirUnclearable(f"could not clear stripped/: {e}") from None

        started = datetime.now(UTC)

        def _run() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for src in originals:
                try:
                    results.append(
                        strip_one_wav(src, out_dir, req.min_silence_ms, req.pad_ms, req.speech_floor_db)
                    )
                except Exception as e:
                    results.append({"name": src.name, "written": False, "error": str(e)})
            return results

        results = await asyncio.to_thread(_run)
        finished = datetime.now(UTC)

        # Persist the committed cut (#90): the explicit spans each written WAV
        # was cut to, keyed by ORIGINAL name, plus the knobs that produced
        # them. Lives inside stripped/ so a re-strip's rmtree or a "clear
        # stripped" wipes it with the clips it describes.
        spans_by_original = {
            r["name"]: r["region_spans"] for r in results if r.get("written") and r.get("region_spans")
        }
        if spans_by_original:
            atomic_write_text(
                out_dir / "strip-meta.json",
                json.dumps(
                    {
                        "stripped_at": finished.isoformat(),
                        "knobs": {
                            "min_silence_ms": req.min_silence_ms,
                            "pad_ms": req.pad_ms,
                            "speech_floor_db": req.speech_floor_db,
                        },
                        "files": spans_by_original,
                    },
                    indent=2,
                ),
            )

    written = sum(1 for r in results if r.get("written"))
    in_secs = sum(r.get("in_seconds", 0.0) for r in results)
    speech_secs = sum(r.get("speech_seconds", 0.0) for r in results)
    detectors = sorted({r.get("detector") for r in results if r.get("detector")})

    print(
        f"[tapscribe] strip-silence {req.session}: {written}/{len(originals)} wavs, "
        f"{speech_secs:.1f}s speech of {in_secs:.1f}s ({100 * speech_secs / max(in_secs, 1e-9):.0f}%), "
        f"detector={detectors}, took {int((finished - started).total_seconds() * 1000)} ms",
        flush=True,
    )

    return {
        "ok": True,
        "session": req.session,
        "files_processed": len(originals),
        "files_written": written,
        "in_seconds": round(in_secs, 2),
        "speech_seconds": round(speech_secs, 2),
        "detector": detectors[0] if len(detectors) == 1 else detectors,
        "stripped_at": finished.isoformat(),
        "took_ms": int((finished - started).total_seconds() * 1000),
        "files": results,
    }
