"""Strip silence from WAV files or split them at silence boundaries.

Detection prefers silero-vad if installed (pip install silero-vad), with a
simple RMS-amplitude threshold fallback otherwise.

Inputs are expected to be 16 kHz mono int16, matching what the recorder
captures from the bridge extension. Use ffmpeg to convert other formats
first.

This module is imported by `tapscribe.sessions` for the operator-triggered
strip-silence endpoint, and is also runnable as a CLI via
`tools/strip_silence_cli.py`.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000

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
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
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
        if rms <= 0:
            continue
        dbfs = 20.0 * math.log10(rms / 32768.0)
        if dbfs >= floor_dbfs:
            out.append((s, e))
    return out


def detect_speech_silero(samples_int16: np.ndarray, min_silence_ms: int, pad_ms: int):
    """Returns list of (start_sample, end_sample) speech regions, or None if
    silero-vad isn't importable."""
    try:
        import torch
        from silero_vad import get_speech_timestamps, load_silero_vad
    except ImportError:
        return None
    audio = torch.from_numpy(samples_int16).float() / 32768.0
    model = load_silero_vad()
    ts = get_speech_timestamps(
        audio, model,
        sampling_rate=SAMPLE_RATE,
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=pad_ms,
    )
    return [(t["start"], t["end"]) for t in ts]


def detect_speech_rms(samples_int16: np.ndarray, threshold_db: float, min_silence_ms: int, pad_ms: int):
    """RMS-threshold fallback when silero-vad is not installed.

    Computes RMS over 30 ms windows, marks anything above threshold as speech,
    merges gaps shorter than min_silence_ms, then pads each region by pad_ms.
    """
    window_ms = 30
    window_samples = SAMPLE_RATE * window_ms // 1000
    audio = samples_int16.astype(np.float32) / 32768.0
    n_windows = len(audio) // window_samples
    if n_windows == 0:
        return []
    audio = audio[:n_windows * window_samples].reshape(n_windows, window_samples)
    rms = np.sqrt((audio ** 2).mean(axis=1) + 1e-12)
    db = 20.0 * np.log10(rms + 1e-12)
    is_speech = db > threshold_db

    regions_w = []
    in_speech = False
    start = 0
    for i, v in enumerate(is_speech):
        if v and not in_speech:
            start = i
            in_speech = True
        elif not v and in_speech:
            regions_w.append((start, i))
            in_speech = False
    if in_speech:
        regions_w.append((start, n_windows))

    min_silence_windows = max(1, min_silence_ms // window_ms)
    merged_w = []
    for s, e in regions_w:
        if merged_w and s - merged_w[-1][1] < min_silence_windows:
            merged_w[-1] = (merged_w[-1][0], e)
        else:
            merged_w.append((s, e))

    pad_samples = (SAMPLE_RATE * pad_ms) // 1000
    total = len(samples_int16)
    padded = []
    for s, e in merged_w:
        s2 = max(0, s * window_samples - pad_samples)
        e2 = min(total, e * window_samples + pad_samples)
        if padded and s2 <= padded[-1][1]:
            padded[-1] = (padded[-1][0], e2)
        else:
            padded.append((s2, e2))
    return padded
