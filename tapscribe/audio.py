"""WAV I/O helpers — duration, RMS, and per-tap meter math for the
recorder format.

The recorder always writes 16 kHz mono int16 PCM; helpers in this
module assume that format. The companion module
`tapscribe.wav_predecode` builds on these constants to skip the
system `ffmpeg` dependency the MLX backends would otherwise spawn —
see its docstring for the why.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path

RECORDER_SAMPLE_RATE = 16000
RECORDER_SAMPLE_WIDTH = 2
RECORDER_CHANNELS = 1


def open_recorder_wav(path: Path) -> wave.Wave_write:
    """Open `path` for writing in the recorder's canonical format
    (16 kHz / mono / int16). The caller closes the returned handle —
    typically via `with` or an explicit `.close()` from a longer-lived
    owner like TapFanOut."""
    wf = wave.open(str(path), "wb")
    wf.setnchannels(RECORDER_CHANNELS)
    wf.setsampwidth(RECORDER_SAMPLE_WIDTH)
    wf.setframerate(RECORDER_SAMPLE_RATE)
    return wf


def dbfs_from_rms(rms: float) -> float:
    """Convert an int16 RMS amplitude to dBFS. Returns -200.0 for silent /
    zero input so callers get a usable sentinel instead of -inf."""
    if rms <= 0:
        return -200.0
    return 20.0 * math.log10(rms / 32768.0)


def int16_peak_norm(buf: bytes) -> float:
    """Normalised peak amplitude (0.0–1.0) of a 16-bit little-endian PCM
    buffer. Used by the per-tap volume meter so the dashboard can show a
    live "audio is coming in" indicator per active stream.

    Returns 0.0 for empty / malformed input — silence and "no buffer" look
    the same on the meter, which is what an operator wants. We divide by
    32768 (not 32767) so the most-negative int16 sample maps to 1.0
    exactly instead of overflowing the bar.

    Odd byte counts are truncated to the largest even prefix instead of
    raising. The bridge always sends 640-byte (320-sample) frames so
    this only matters for callers that bypass the wire (tests, future
    framers, malformed network reads) — but a crash here would tear
    down the whole `/tap` WebSocket, so we'd rather under-report by
    one sample than abort the stream."""
    if len(buf) < 2:
        return 0.0
    import numpy as np

    # `np.frombuffer` requires the byte count to be a multiple of the
    # element size (2 for int16); pass a slice that is.
    even_len = len(buf) & ~1
    samples = np.frombuffer(buf[:even_len], dtype=np.int16)
    if samples.size == 0:
        return 0.0
    lo = int(samples.min())
    hi = int(samples.max())
    peak = -lo if -lo > hi else hi
    return peak / 32768.0


def wav_duration_s(path: Path) -> float:
    """Return WAV duration in seconds. Returns 0.0 on read errors so callers
    can use this as a cheap is-corrupt check."""
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            return frames / rate
    except (wave.Error, OSError, EOFError):
        return 0.0


def wav_rms_dbfs(path: Path) -> float:
    """Whole-file RMS amplitude in dBFS.

    Returns 0.0 on read errors so callers fail OPEN (file gets transcribed
    normally and any real problem surfaces downstream). Returns -200.0
    (effectively -inf) for empty/zero files so callers can treat them as
    silent.

    Used by the transcribe path as a cheap "is this WAV worth running
    Whisper on?" check. Whole-file RMS is conservative — a 60s file with 1s
    of speech + 59s of silence still measures around -38 dBFS, well above
    any sensible silence threshold. Files measuring below -50 dBFS have no
    sustained signal and Whisper hallucinates on them.
    """
    import numpy as np  # transitive dep via faster-whisper / mlx-whisper

    try:
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2:
                return 0.0
            raw = w.readframes(w.getnframes())
    except (wave.Error, OSError, EOFError):
        return 0.0
    if not raw:
        return -200.0
    samples = np.frombuffer(raw, dtype=np.int16)
    if len(samples) == 0:
        return -200.0
    rms = float(np.sqrt((samples.astype(np.float32) ** 2).mean()))
    return dbfs_from_rms(rms)
