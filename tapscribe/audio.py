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
from dataclasses import dataclass
from pathlib import Path

RECORDER_SAMPLE_RATE = 16000
RECORDER_SAMPLE_WIDTH = 2
RECORDER_CHANNELS = 1


def read_recorder_frames(path: Path) -> tuple[bytes, int]:
    """Read every PCM frame from a recorder-format WAV, validating the
    format first. Returns `(raw_bytes, frame_count)`.

    Raises `RuntimeError` — naming the file and the actual vs expected
    format, plus a one-line ffmpeg recipe — on a non-recorder WAV
    (different rate / channels / sample width). There is NO ffmpeg
    fallback and NO resample anywhere in the codebase (see
    `tapscribe.wav_predecode`'s module docstring): the error is the
    operator's signal to convert the file. `wave.Error` / `OSError` /
    `EOFError` from an unreadable file propagate for the caller to
    wrap or forward. Shared by `compute_peaks`,
    `wav_predecode.load_recorder_wav_as_pcm`, and
    `strip_silence.read_wav_int16` (which translates the RuntimeError
    to ValueError for the strip routes' 422 contract) so the guard (and
    its message) can't drift between them."""
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        if (
            rate != RECORDER_SAMPLE_RATE
            or channels != RECORDER_CHANNELS
            or sampwidth != RECORDER_SAMPLE_WIDTH
        ):
            raise RuntimeError(
                f"unexpected WAV format for {path.name}: "
                f"{rate}Hz/{channels}ch/{sampwidth * 8}-bit "
                "(expected 16kHz/mono/16-bit — TapScribe writes that natively). "
                "Convert with: ffmpeg -i in.wav -ar 16000 -ac 1 -sample_fmt s16 out.wav"
            )
        frame_count = wf.getnframes()
        raw = wf.readframes(frame_count)
        # WHOLE FRAMES ONLY. A WAV truncated mid-sample (a partial flush on
        # ENOSPC, a device error mid-write) yields an odd byte count, and every
        # consumer here feeds the result straight to `np.frombuffer(...,
        # int16)`, which raises `ValueError: buffer size must be a multiple of
        # element size` — bypassing each caller's documented error contract.
        # Truncating in the ONE shared reader is what this function is for: the
        # guard cannot then drift between `compute_peaks`,
        # `wav_predecode.load_recorder_wav_as_pcm` and
        # `strip_silence.read_wav_int16`, and a fourth consumer inherits it.
        frame_bytes = RECORDER_SAMPLE_WIDTH * RECORDER_CHANNELS
        usable = len(raw) - (len(raw) % frame_bytes)
        return raw[:usable], min(frame_count, usable // frame_bytes)


def _configure_recorder_format(wf: wave.Wave_write) -> None:
    """Apply the recorder's canonical PCM format (16 kHz / mono / int16) to a
    freshly opened `Wave_write`. Shared by `open_recorder_wav` and the append
    path (`wav_append.open_recorder_wav_append`) so the format lives in one
    place — a future format tweak can't leave the two setups divergent."""
    wf.setnchannels(RECORDER_CHANNELS)
    wf.setsampwidth(RECORDER_SAMPLE_WIDTH)
    wf.setframerate(RECORDER_SAMPLE_RATE)


def open_recorder_wav(path: Path) -> wave.Wave_write:
    """Open `path` for writing in the recorder's canonical format
    (16 kHz / mono / int16). The caller closes the returned handle —
    typically via `with` or an explicit `.close()` from a longer-lived
    owner like TapFanOut."""
    wf = wave.open(str(path), "wb")
    _configure_recorder_format(wf)
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
    # Truncate to whole int16s: a WAV whose data chunk is an ODD byte count
    # (a partial flush on ENOSPC, a device error mid-write) makes
    # `np.frombuffer` raise ValueError, which this function's `except` does not
    # catch — so the documented "Returns 0.0 on read errors so callers fail
    # OPEN" contract silently didn't hold, and `_precheck_original` 500'd
    # instead of returning its WavTooQuiet/WavUnreadable 422. Same `& ~1` guard
    # `int16_peak_norm` already applies above, for the same reason.
    samples = np.frombuffer(raw[: len(raw) & ~1], dtype=np.int16)
    if len(samples) == 0:
        return -200.0
    rms = float(np.sqrt((samples.astype(np.float32) ** 2).mean()))
    return dbfs_from_rms(rms)


@dataclass(frozen=True)
class WavePeaks:
    """A fixed-size downsample of one recording for the dashboard waveform:
    `bins` normalised peak amplitudes in [0, 1] (one per bucket, the same
    `|sample| / 32768` normalisation `int16_peak_norm` gives the live meter),
    plus the duration / sample rate the renderer needs for a time axis.

    The point of computing this server-side is that the wire payload is
    `bins` floats regardless of how long the recording is — a 3-minute and a
    3-hour WAV both downsample to the same small array."""

    peaks: list[float]
    bins: int
    duration_s: float
    sample_rate: int


def compute_peaks(path: Path, *, bins: int) -> WavePeaks:
    """Downsample the recorder's WAV (16 kHz mono int16) into `bins` buckets,
    each carrying that bucket's normalised peak amplitude — `max(|min|, |max|)
    / 32768`, clamped to 1.0 for the most-negative int16 sample, exactly the
    contract `int16_peak_norm` gives the per-tap meter.

    `bins` is coerced to at least 1. A WAV with fewer samples than `bins`
    still returns exactly `bins` entries (the trailing empty buckets read
    0.0), so the renderer can rely on a fixed-length array.

    Raises `RuntimeError` — naming the file and the actual vs expected format —
    on a non-recorder WAV (different rate / channels / sample width) or an
    unreadable file. There is NO ffmpeg fallback and NO resample: this path
    assumes the recorder format, mirroring `tapscribe.wav_predecode`, and
    tells the operator to convert anything else rather than silently
    re-introducing the dependency.
    """
    import numpy as np  # transitive dep via faster-whisper / mlx-whisper

    bins = max(1, int(bins))
    try:
        # Shared format guard (and message) with wav_predecode.load_recorder_wav_as_pcm.
        raw, frame_count = read_recorder_frames(path)
    except (wave.Error, EOFError, OSError) as e:
        raise RuntimeError(f"could not read WAV {path.name}: {e}") from e

    # read_recorder_frames validated the rate == RECORDER_SAMPLE_RATE.
    duration_s = frame_count / RECORDER_SAMPLE_RATE
    # `read_recorder_frames` guarantees a whole number of frames, so this
    # cannot raise on a torn WAV.
    samples = np.frombuffer(raw, dtype=np.int16)
    if samples.size == 0:
        return WavePeaks(
            peaks=[0.0] * bins, bins=bins, duration_s=duration_s, sample_rate=RECORDER_SAMPLE_RATE
        )

    # Per bucket: max(|min|, |max|) / 32768. Working from the signed min/max
    # (not np.abs) sidesteps the int16 overflow where abs(-32768) stays
    # -32768, and maps a full-scale negative sample to exactly 1.0 — the same
    # trick int16_peak_norm uses. np.array_split always returns `bins`
    # sub-arrays (trailing ones empty when samples < bins), so len == bins.
    peaks: list[float] = []
    for bucket in np.array_split(samples, bins):
        if bucket.size == 0:
            peaks.append(0.0)
            continue
        lo = int(bucket.min())
        hi = int(bucket.max())
        peak = -lo if -lo > hi else hi
        peaks.append(peak / 32768.0)

    return WavePeaks(peaks=peaks, bins=len(peaks), duration_s=duration_s, sample_rate=RECORDER_SAMPLE_RATE)
