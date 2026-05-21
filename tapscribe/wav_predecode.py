"""Pre-decode the recorder's WAV into a float32 PCM array, skipping
the system `ffmpeg` binary the MLX model loaders would otherwise spawn.

Why this module exists
----------------------
Both `mlx-whisper` and `parakeet-mlx` ship audio loaders that shell out
to `ffmpeg` whenever you hand them a file path. On a fresh checkout
without `brew install ffmpeg`, the recorder boots fine but every batch
transcribe fails at request time with::

    RuntimeError: FFmpeg is not installed or not in your PATH.

…deep inside Starlette middleware. We side-step the dependency by
decoding the recorder's WAV ourselves and handing the model a raw PCM
array. Both backends expose a documented entry point for that:

- `mlx_whisper.transcribe(audio_array, …)` — accepts a float32 numpy
  array directly when you give it one instead of a path.
- `parakeet_mlx`'s model exposes `generate(mel)` where `mel` comes
  from `parakeet_mlx.audio.get_logmel(audio, preproc)`.

The trick only works because the recorder writes its WAVs in the
single format both models want internally: **16 kHz / mono / int16**.
There's no resampling or channel mixing to do, so a `wave.open` +
`np.frombuffer` + `/32768.0` is the entire decode.

If a future loader needs a different sample rate or channel layout,
`load_recorder_wav_as_pcm` raises and the caller falls back to the
ffmpeg-backed path. Don't grow this module into a general-purpose
audio decoder — keep it laser-focused on the recorder format, since
the whole point is that we know exactly what's on disk.
"""

from __future__ import annotations

import wave
from pathlib import Path

from .audio import RECORDER_CHANNELS, RECORDER_SAMPLE_RATE, RECORDER_SAMPLE_WIDTH


def load_recorder_wav_as_pcm(path: Path):
    """Decode the recorder's WAV (16 kHz mono int16) into a normalised
    float32 numpy array suitable for passing directly to
    `mlx_whisper.transcribe(array, …)` or to
    `parakeet_mlx.audio.get_logmel(mx.array(array), preproc)`.

    Raises `RuntimeError` if the WAV isn't in the recorder's expected
    format — callers should fall back to their backend's own
    path-based loader (which needs ffmpeg) for unusual inputs.

    See the module docstring for why this exists.
    """
    import numpy as np  # transitive dep via faster-whisper / mlx-whisper

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
                "(expected 16kHz/mono/16-bit — TapScribe writes that natively)"
            )
        frames = wf.readframes(wf.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
