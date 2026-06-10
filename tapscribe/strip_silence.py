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

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config
from .audio import RECORDER_SAMPLE_RATE as SAMPLE_RATE
from .audio import dbfs_from_rms, open_recorder_wav

# Per-region amplitude floor for what counts as actual speech. Below this,
# regions are usually room noise / HVAC / faint breathing that silero-vad
# mis-classifies as speech — Whisper then hallucinates plausible English
# subtitles on them. -45 dBFS leaves plenty of headroom for soft voices
# while ruling out ambient noise floor.
SPEECH_RMS_DBFS_FLOOR = -45.0


def read_wav_int16(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE:
            raise ValueError(f"{path}: expected {SAMPLE_RATE} Hz, got {w.getframerate()}")
        if w.getnchannels() != 1:
            raise ValueError(f"{path}: expected mono, got {w.getnchannels()} channels")
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: expected int16, got sampwidth {w.getsampwidth()}")
        raw = w.readframes(w.getnframes())
    # .copy() so the returned array is writable. np.frombuffer returns a
    # read-only view of the bytes buffer, and torch.from_numpy(...) on a
    # read-only array emits a noisy "non-writable" warning every run.
    return np.frombuffer(raw, dtype=np.int16).copy()


def write_wav_int16(path: Path, samples: np.ndarray) -> None:
    with open_recorder_wav(path) as w:
        w.writeframes(samples.astype(np.int16).tobytes())


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
        rms = float(np.sqrt((region.astype(np.float32) ** 2).mean()))
        if dbfs_from_rms(rms) >= floor_dbfs:
            out.append((s, e))
    return out


def detect_speech_silero(samples_int16: np.ndarray, min_silence_ms: int, pad_ms: int):
    """Returns list of (start_sample, end_sample) speech regions.

    silero-vad + torch are core dependencies (pyproject.toml). If the
    import below ever fails, the install is broken — reinstall TapScribe.
    Raised as RuntimeError so the route surfaces a clear 500 instead of
    a bare ImportError.
    """
    try:
        import torch
        from silero_vad import get_speech_timestamps, load_silero_vad
    except ImportError as e:
        raise RuntimeError(
            "silero-vad/torch import failed — TapScribe install is corrupt. "
            "Reinstall the package (`pip install -e .`)."
        ) from e
    audio = torch.from_numpy(samples_int16).float() / 32768.0
    model = load_silero_vad()
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
    bounds the splitter writes from; `spans` are the same boundaries as
    rounded {start_s, end_s} second-dicts (the wire/UI shape). Empty
    `regions` with a non-None `reason` explains why nothing would be
    written; `detector` is None when detection never ran (empty or
    whole-file-silent input)."""

    regions: list[tuple[int, int]]
    spans: list[dict[str, float]]
    in_seconds: float
    speech_seconds: float
    segments_filtered_below_floor: int
    silent: bool
    rms_dbfs: float
    reason: str | None
    detector: str | None


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
        return StripPlan(
            regions=[], spans=[], in_seconds=0.0, speech_seconds=0.0,
            segments_filtered_below_floor=0, silent=True, rms_dbfs=-200.0,
            reason="empty", detector=None,
        )

    rms = float(np.sqrt((samples.astype(np.float32) ** 2).mean()))
    rms_dbfs = dbfs_from_rms(rms)
    if rms_dbfs < config.SILENT_RMS_DBFS_FLOOR:
        return StripPlan(
            regions=[], spans=[], in_seconds=round(in_secs, 2), speech_seconds=0.0,
            segments_filtered_below_floor=0, silent=True, rms_dbfs=rms_dbfs,
            reason=f"whole-file silent ({rms_dbfs:.1f} dBFS RMS, floor {config.SILENT_RMS_DBFS_FLOOR} dBFS)",
            detector=None,
        )

    regions = detect_speech_silero(samples, min_silence_ms=min_silence_ms, pad_ms=pad_ms)
    if not regions:
        return StripPlan(
            regions=[], spans=[], in_seconds=round(in_secs, 2), speech_seconds=0.0,
            segments_filtered_below_floor=0, silent=False, rms_dbfs=rms_dbfs,
            reason="no speech detected", detector="silero-vad",
        )

    pre_filter_count = len(regions)
    regions = filter_low_energy_regions(samples, regions, floor_dbfs=speech_floor_db)
    if not regions:
        return StripPlan(
            regions=[], spans=[], in_seconds=round(in_secs, 2), speech_seconds=0.0,
            segments_filtered_below_floor=pre_filter_count, silent=False, rms_dbfs=rms_dbfs,
            reason=f"all {pre_filter_count} regions below {speech_floor_db:.1f} dBFS speech floor",
            detector="silero-vad",
        )

    spans = [
        {"start_s": round(s / SAMPLE_RATE, 3), "end_s": round(e / SAMPLE_RATE, 3)}
        for s, e in regions
    ]
    speech_samples = sum(e - s for s, e in regions)
    return StripPlan(
        regions=regions, spans=spans, in_seconds=round(in_secs, 2),
        speech_seconds=round(speech_samples / SAMPLE_RATE, 2),
        segments_filtered_below_floor=pre_filter_count - len(regions),
        silent=False, rms_dbfs=rms_dbfs, reason=None, detector="silero-vad",
    )
