"""Strip silence from WAV files or split them at silence boundaries.

Detection runs through silero-vad, which ships as a core dependency
(see pyproject.toml). It's the same engine the live SpeechGate uses.

Inputs are expected to be 16 kHz mono int16, matching what the recorder
captures from the bridge extension. Use ffmpeg to convert other formats
first.

This module is imported by `tapscribe.sessions` for the operator-triggered
strip-silence endpoint, and is also runnable as a CLI via
`tools/strip_silence_cli.py`.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import config
from .audio import RECORDER_SAMPLE_RATE as SAMPLE_RATE
from .audio import dbfs_from_rms, open_recorder_wav, read_recorder_frames

# Per-region amplitude floor for what counts as actual speech. Below this,
# regions are usually room noise / HVAC / faint breathing that silero-vad
# mis-classifies as speech — Whisper then hallucinates plausible English
# subtitles on them. -45 dBFS leaves plenty of headroom for soft voices
# while ruling out ambient noise floor.
SPEECH_RMS_DBFS_FLOOR = -45.0


def read_wav_int16(path: Path) -> np.ndarray:
    """Read a recorder-format WAV into a writable int16 sample array.

    Delegates the open / format-validate / readframes sequence to
    `audio.read_recorder_frames` — the ONE recorder-format guard (shared
    with `compute_peaks` and `wav_predecode`) — translating its
    format-mismatch `RuntimeError` into `ValueError`: the strip-preview
    route maps `(ValueError, wave.Error, EOFError, OSError)` to a 422
    and documents this function raising ValueError for non-recorder
    formats, so the translation keeps that contract (and the callers'
    exception handling) intact while removing the duplicated guard.
    `wave.Error` / `EOFError` / `OSError` from unreadable files
    propagate unchanged, as before."""
    try:
        raw, _ = read_recorder_frames(path)
    except RuntimeError as e:
        raise ValueError(str(e)) from e
    # .copy() so the returned array is writable. np.frombuffer returns a
    # read-only view of the bytes buffer, and torch.from_numpy(...) on a
    # read-only array emits a noisy "non-writable" warning every run.
    return np.frombuffer(raw, dtype=np.int16).copy()


def write_wav_int16(path: Path, samples: np.ndarray) -> None:
    with open_recorder_wav(path) as w:
        w.writeframes(samples.astype(np.int16).tobytes())


def rms_dbfs_int16(samples_int16: np.ndarray) -> float:
    """RMS amplitude of int16 samples in dBFS. Single pass with a float64
    accumulator (einsum) — no full-length float temporaries, which matters at
    the whole-file sizes the strip planner and preview route hand in (an
    hour of 16 kHz audio would otherwise materialise two ~460 MB float32
    copies per call). Callers guard against empty input."""
    mean_sq = float(np.einsum("i,i->", samples_int16, samples_int16, dtype=np.float64)) / len(samples_int16)
    return dbfs_from_rms(math.sqrt(mean_sq))


def filter_low_energy_regions(samples_int16: np.ndarray, regions, floor_dbfs: float = SPEECH_RMS_DBFS_FLOOR):
    """Drop regions whose RMS amplitude is below floor_dbfs.

    silero-vad has good per-frame speech probability but no energy gate, so
    sustained ambient noise (HVAC, traffic, breathing) can come back as
    'speech regions'. Whisper then hallucinates plausible English on them.
    Filtering here ensures the stripped output only contains audio loud
    enough to plausibly carry actual speech.
    """
    out = []
    for s, e in regions:
        region = samples_int16[s:e]
        if len(region) == 0:
            continue
        if rms_dbfs_int16(region) >= floor_dbfs:
            out.append((s, e))
    return out


# One Silero model PER WORKER THREAD for the strip detector. Silero's
# ONNX wrapper carries its recurrent streaming state on the MODEL object
# (see `speech_gate.load_silero_model`), so strip ops must never share an
# instance with the live gates' per-gate models OR with each other:
# a strip-preview knob drag / batch strip running `get_speech_timestamps`
# on a shared model would zero and rewrite a live gate's state
# mid-utterance from another thread. `threading.local` gives each
# `asyncio.to_thread` worker its own instance, and use within a thread is
# strictly sequential — a worker thread runs one work item at a time and
# `detect_speech_silero` never re-enters itself, so no two detections can
# interleave on one instance (and `get_speech_timestamps` resets model
# state on entry anyway). Worker threads are reused across knob drags and
# batch runs, so loads amortize instead of costing a model
# deserialisation per slider pause.
_SILERO_LOCAL = threading.local()


def _local_silero_model():
    """This thread's own Silero model, loaded on first use (see the
    `_SILERO_LOCAL` rationale above). The lazy `speech_gate` import keeps
    silero/onnx out of this module's import graph; an ImportError
    propagates to `detect_speech_silero`'s broken-install wrapping."""
    model = getattr(_SILERO_LOCAL, "model", None)
    if model is None:
        from .speech_gate import load_silero_model

        model = load_silero_model()
        _SILERO_LOCAL.model = model
    return model


def detect_speech_silero(samples_int16: np.ndarray, min_silence_ms: int, pad_ms: int):
    """Returns list of (start_sample, end_sample) speech regions.

    The model is this worker thread's own instance (`_local_silero_model`)
    — NEVER the live SpeechGates' per-gate instances, because silero's
    streaming state lives on the model object (the `_SILERO_LOCAL`
    rationale above; the onnx=True rationale lives on
    `speech_gate.load_silero_model`). The strip-preview route runs this
    detector per knob pause; the per-thread cache keeps the model's
    deserialisation cost off every slider drag. Imported lazily so
    importing this module never pulls silero/onnx.

    silero-vad + torch are core dependencies (pyproject.toml). If the
    import below ever fails, the install is broken — reinstall TapScribe.
    Raised as RuntimeError so the route surfaces a clear 500 instead of
    a bare ImportError.
    """
    try:
        import torch
        from silero_vad import get_speech_timestamps

        model = _local_silero_model()
    except ImportError as e:
        raise RuntimeError(
            "silero-vad/torch import failed — TapScribe install is corrupt. "
            "Reinstall the package (`pip install -e .`)."
        ) from e
    audio = torch.from_numpy(samples_int16).float() / 32768.0
    ts = get_speech_timestamps(
        audio,
        model,
        sampling_rate=SAMPLE_RATE,
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=pad_ms,
    )
    return [(t["start"], t["end"]) for t in ts]


@dataclass(frozen=True)
class StripPlan:
    """What a strip-silence run WOULD do to one WAV's samples — the padded,
    floor-filtered speech regions plus the aggregate stats and the
    whole-file-silence verdict. Pure data, no disk writes: `strip_one_wav`
    writes one region file per entry in `regions`, and the strip-preview
    route serialises `spans` + the stats for the dashboard's live overlay.

    `regions` are (start_sample, end_sample) tuples — the exact slice
    bounds the splitter writes from; `spans` derives the same boundaries
    as rounded {start_s, end_s} second-dicts (the wire/UI shape). Empty
    `regions` with a non-None `reason` explains why nothing would be
    written; `detector` is None when detection never ran (empty or
    whole-file-silent input). The defaults are the empty-plan values, so
    the planner's early exits state only what differs."""

    in_seconds: float
    silent: bool
    rms_dbfs: float
    regions: list[tuple[int, int]] = field(default_factory=list)
    speech_seconds: float = 0.0
    segments_filtered_below_floor: int = 0
    reason: str | None = None
    detector: str | None = None

    @property
    def spans(self) -> list[dict[str, float]]:
        """`regions` as the wire/UI shape — derived, so the two
        representations can never drift."""
        return [
            {"start_s": round(s / SAMPLE_RATE, 3), "end_s": round(e / SAMPLE_RATE, 3)}
            for s, e in self.regions
        ]


def plan_strip_regions(
    samples: np.ndarray,
    *,
    min_silence_ms: int,
    pad_ms: int,
    speech_floor_db: float = SPEECH_RMS_DBFS_FLOOR,
) -> StripPlan:
    """Plan the strip-silence cut for one WAV's samples — extracted from
    `batch_strip.strip_one_wav` (#89) so the live strip-preview endpoint and
    the splitter share one detection path. Behaviour-preserving: the same
    whole-file silence gate (RMS vs `config.SILENT_RMS_DBFS_FLOOR`, computed
    from the samples instead of a second file read), the same silero
    detection, the same per-region energy floor — in the same order."""
    total = len(samples)
    in_secs = total / SAMPLE_RATE
    if total == 0:
        return StripPlan(in_seconds=0.0, silent=True, rms_dbfs=-200.0, reason="empty")

    rms_dbfs = rms_dbfs_int16(samples)
    if rms_dbfs < config.SILENT_RMS_DBFS_FLOOR:
        return StripPlan(
            in_seconds=round(in_secs, 2),
            silent=True,
            rms_dbfs=rms_dbfs,
            reason=f"whole-file silent ({rms_dbfs:.1f} dBFS RMS, floor {config.SILENT_RMS_DBFS_FLOOR} dBFS)",
        )

    regions = detect_speech_silero(samples, min_silence_ms=min_silence_ms, pad_ms=pad_ms)
    if not regions:
        return StripPlan(
            in_seconds=round(in_secs, 2),
            silent=False,
            rms_dbfs=rms_dbfs,
            reason="no speech detected",
            detector="silero-vad",
        )

    pre_filter_count = len(regions)
    regions = filter_low_energy_regions(samples, regions, floor_dbfs=speech_floor_db)
    if not regions:
        return StripPlan(
            in_seconds=round(in_secs, 2),
            silent=False,
            rms_dbfs=rms_dbfs,
            segments_filtered_below_floor=pre_filter_count,
            reason=f"all {pre_filter_count} regions below {speech_floor_db:.1f} dBFS speech floor",
            detector="silero-vad",
        )

    speech_samples = sum(e - s for s, e in regions)
    return StripPlan(
        in_seconds=round(in_secs, 2),
        silent=False,
        rms_dbfs=rms_dbfs,
        regions=regions,
        speech_seconds=round(speech_samples / SAMPLE_RATE, 2),
        segments_filtered_below_floor=pre_filter_count - len(regions),
        detector="silero-vad",
    )
