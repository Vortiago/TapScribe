"""Strip silence from WAV files or split them at silence boundaries.

Detection runs through silero-vad (install via the `vad` extra:
`pip install tapscribe[vad]`). It's the same engine the live SpeechGate
uses, so any TapScribe install that runs the live channel already has it.

Inputs are expected to be 16 kHz mono int16, matching what the recorder
captures from the bridge extension. Use ffmpeg to convert other formats
first.

This module is imported by `tapscribe.sessions` for the operator-triggered
strip-silence endpoint, and is also runnable as a CLI via
`tools/strip_silence_cli.py`.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

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

    Raises RuntimeError if silero-vad isn't installed — the operator needs
    to `pip install tapscribe[vad]`. There's no RMS fallback: the live
    SpeechGate has the same dependency, so any working TapScribe install
    already has silero.
    """
    try:
        import torch
        from silero_vad import get_speech_timestamps, load_silero_vad
    except ImportError as e:
        raise RuntimeError(
            "strip-silence requires silero-vad. Install it with `pip install tapscribe[vad]`."
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
