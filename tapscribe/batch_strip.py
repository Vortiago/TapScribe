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

from . import strip_silence as _ss
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
    plan = _ss.plan_strip_regions(
        samples,
        min_silence_ms=min_silence_ms,
        pad_ms=pad_ms,
        speech_floor_db=speech_floor_db,
    )

    if not plan.regions:
        row: dict[str, Any] = {
            "name": src.name,
            "in_seconds": plan.in_seconds,
            "speech_seconds": 0.0,
            "segments": 0,
            "written": False,
            "regions_written": [],
            "reason": plan.reason,
        }
        if plan.detector:
            row["detector"] = plan.detector
        return row

    out_dir.mkdir(parents=True, exist_ok=True)
    origin = parse_wav_start(src.name) or datetime.fromtimestamp(src.stat().st_mtime, tz=UTC)
    speaker_slug, ident_slug = parse_wav_speaker_ident(src.name)

    speech_samples = 0
    regions_written: list[str] = []
    region_spans: list[dict[str, Any]] = []
    for (start_sample, end_sample), span in zip(plan.regions, plan.spans, strict=True):
        region_samples = samples[start_sample:end_sample]
        offset_s = start_sample / _ss.SAMPLE_RATE
        region_start = origin + timedelta(seconds=offset_s)
        fname = build_recorder_wav_name(region_start, speaker_slug, ident_slug)
        _ss.write_wav_int16(out_dir / fname, region_samples)
        regions_written.append(fname)
        region_spans.append({"name": fname, **span})
        speech_samples += len(region_samples)

    return {
        "name": src.name,
        "in_seconds": plan.in_seconds,
        "speech_seconds": round(speech_samples / _ss.SAMPLE_RATE, 2),
        "segments": len(plan.regions),
        "segments_filtered_below_floor": plan.segments_filtered_below_floor,
        "written": True,
        "regions_written": regions_written,
        "region_spans": region_spans,
        "detector": plan.detector,
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

        # Persist the committed cut (#90, schema v2): per ORIGINAL, the
        # explicit spans each region clip was cut to (each span carries its
        # clip's filename so a single-clip delete can prune it) plus a
        # size/mtime fingerprint of the original (a since-rewritten WAV must
        # read as "no committed cut", not draw stale geometry). Lives inside
        # stripped/ so a re-strip's rmtree or a "clear stripped" wipes it
        # with the clips it describes.
        spans_by_original: dict[str, dict[str, Any]] = {}
        for r in results:
            if not (r.get("written") and r.get("region_spans")):
                continue
            try:
                st = (session_dir / r["name"]).stat()
            except OSError:
                continue
            spans_by_original[r["name"]] = {
                "wav_size": st.st_size,
                "wav_mtime_ns": st.st_mtime_ns,
                "spans": r["region_spans"],
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
