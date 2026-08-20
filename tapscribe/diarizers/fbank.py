"""Kaldi-compatible 80-bin log-mel fbank, in numpy.

The speaker-embedding model was trained on Kaldi's fbank, so its frontend is
part of the model's contract — a subtly wrong window or mel edge still yields
512 plausible numbers. This is a port of the settings sherpa-onnx feeds these
models with, pinned against `kaldi-native-fbank` by
`tests/test_diarize_fbank.py`.

numpy only: `onnxruntime` is already a core dependency but torch is not (#374),
and `kaldi-native-fbank` has no cp314 Windows wheel while CI runs py3.14 there
— so it stays a test-time oracle, exactly as `silero-vad` does for
`tapscribe/vad/`.
"""

from __future__ import annotations

import numpy as np

#: The rate the embedding model was trained at, and what `reference.npz` is
#: computed from — NOT `audio.RECORDER_SAMPLE_RATE`. Same number, different
#: fact: a /tap rate change must not silently retune the mel edges below.
#: Mirrors `vad/silero.py`'s `SUPPORTED_RATE` for the same reason.
SUPPORTED_RATE = 16000
NUM_BINS = 80
FRAME_LENGTH = 400  # 25 ms
FRAME_SHIFT = 160  # 10 ms
FFT_SIZE = 512  # next power of two >= FRAME_LENGTH
PREEMPH = 0.97
LOW_FREQ = 20.0
#: Kaldi floors the mel energy at FLT_EPSILON before the log, so an empty band
#: reads -15.94 rather than -inf. Not FLT_MIN: that would floor at -87, and the
#: low bins — which sit below epsilon on most speech after preemphasis and the
#: 20 Hz cut — would carry 70 dB of noise the model never saw in training.
LOG_FLOOR = float(np.finfo(np.float32).eps)


def _povey_window() -> np.ndarray:
    """Kaldi's default window: Hamming raised to 0.85."""
    n = np.arange(FRAME_LENGTH)
    hann = 0.5 - 0.5 * np.cos(2 * np.pi * n / (FRAME_LENGTH - 1))
    return np.power(hann, 0.85).astype(np.float64)


def _mel_banks() -> np.ndarray:
    """`(NUM_BINS, FFT_SIZE // 2 + 1)` triangular mel filters.

    Kaldi places bin edges on a mel scale spanning `LOW_FREQ` to Nyquist and
    weights each FFT bin by its position between the neighbouring centres — it
    does NOT area-normalise, which is where a Slaney-style bank would diverge.
    """

    def to_mel(f: np.ndarray | float) -> np.ndarray | float:
        return 1127.0 * np.log(1.0 + np.asarray(f) / 700.0)

    nyquist = SUPPORTED_RATE / 2.0
    mel_low, mel_high = to_mel(LOW_FREQ), to_mel(nyquist)
    # NUM_BINS + 2 edges: each filter spans one left/centre/right triple.
    edges = np.linspace(mel_low, mel_high, NUM_BINS + 2)
    num_fft_bins = FFT_SIZE // 2 + 1
    fft_mel = to_mel(np.arange(num_fft_bins) * (SUPPORTED_RATE / FFT_SIZE))

    banks = np.zeros((NUM_BINS, num_fft_bins), dtype=np.float64)
    for i in range(NUM_BINS):
        left, centre, right = edges[i], edges[i + 1], edges[i + 2]
        rising = (fft_mel - left) / (centre - left)
        falling = (right - fft_mel) / (right - centre)
        banks[i] = np.maximum(0.0, np.minimum(rising, falling))
    return banks


_WINDOW = _povey_window()
_BANKS = _mel_banks()


def _frames(samples: np.ndarray) -> np.ndarray:
    """`snip_edges=False` framing: one frame per hop, each centred on its hop,
    with out-of-range samples mirrored.

    The `snip_edges=True` alternative yields fewer frames AND shifts every one
    of them by half a window, so the choice is not cosmetic — it changes which
    audio each frame describes.
    """
    n = len(samples)
    num_frames = int((n + FRAME_SHIFT // 2) // FRAME_SHIFT)
    starts = np.arange(num_frames) * FRAME_SHIFT + FRAME_SHIFT // 2 - FRAME_LENGTH // 2
    idx = starts[:, None] + np.arange(FRAME_LENGTH)[None, :]
    # Mirror at both edges: j < 0 -> -j - 1, j >= n -> 2n - j - 1.
    idx = np.where(idx < 0, -idx - 1, idx)
    idx = np.where(idx >= n, 2 * n - idx - 1, idx)
    return samples[np.clip(idx, 0, n - 1)].astype(np.float64)


def fbank(samples: np.ndarray) -> np.ndarray:
    """`(frames, 80)` float32 log-mel energies for 16 kHz mono float samples."""
    if len(samples) < 1:
        return np.zeros((0, NUM_BINS), dtype=np.float32)

    frames = _frames(np.asarray(samples, dtype=np.float64))
    # Kaldi's order: remove the per-frame DC offset, preemphasise (the first
    # sample against itself), then window.
    frames = frames - frames.mean(axis=1, keepdims=True)
    frames[:, 1:] -= PREEMPH * frames[:, :-1]
    frames[:, 0] -= PREEMPH * frames[:, 0]
    frames *= _WINDOW

    spectrum = np.abs(np.fft.rfft(frames, n=FFT_SIZE)) ** 2
    return np.log(np.maximum(spectrum @ _BANKS.T, LOG_FLOOR)).astype(np.float32)
