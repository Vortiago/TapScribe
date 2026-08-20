"""Kaldi-compatible 80-bin log-mel fbank, in numpy.

The frontend is part of the embedding model's contract: get it subtly wrong and
the model still returns plausible numbers. Pinned against `kaldi-native-fbank`
by `tests/test_diarize_fbank.py` — a test-time oracle only, since it has no
cp314 Windows wheel and CI runs py3.14 there.
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

#: The model's training rate, NOT `audio.RECORDER_SAMPLE_RATE` — same number,
#: different fact, so a /tap rate change must not retune the mel edges.
SUPPORTED_RATE = 16000
NUM_BINS = 80
FRAME_LENGTH = 400  # 25 ms
FRAME_SHIFT = 160  # 10 ms
FFT_SIZE = 512  # next power of two >= FRAME_LENGTH
PREEMPH = 0.97
LOW_FREQ = 20.0
#: Kaldi floors mel energy at FLT_EPSILON, not FLT_MIN: an empty band reads
#: -15.94, and FLT_MIN's -87 would put 70 dB of noise in the low bins.
LOG_FLOOR = float(np.finfo(np.float32).eps)


def _povey_window() -> np.ndarray:
    """Kaldi's `povey` window: Hann^0.85. Not Hamming, which never reaches zero
    at the edges."""
    n = np.arange(FRAME_LENGTH)
    hann = 0.5 - 0.5 * np.cos(2 * np.pi * n / (FRAME_LENGTH - 1))
    return np.power(hann, 0.85)


def _mel_banks() -> np.ndarray:
    """`(NUM_BINS, FFT_SIZE // 2 + 1)` triangular mel filters, NOT
    area-normalised — where a Slaney-style bank diverges."""

    def to_mel(f):
        return 1127.0 * np.log(1.0 + f / 700.0)

    nyquist = SUPPORTED_RATE / 2.0
    mel_low, mel_high = to_mel(LOW_FREQ), to_mel(nyquist)
    # NUM_BINS + 2 edges: each filter spans one left/centre/right triple.
    edges = np.linspace(mel_low, mel_high, NUM_BINS + 2)
    num_fft_bins = FFT_SIZE // 2 + 1
    fft_mel = to_mel(np.arange(num_fft_bins) * (SUPPORTED_RATE / FFT_SIZE))

    left, centre, right = edges[:-2, None], edges[1:-1, None], edges[2:, None]
    rising = (fft_mel - left) / (centre - left)
    falling = (right - fft_mel) / (right - centre)
    return np.maximum(0.0, np.minimum(rising, falling))


_WINDOW = _povey_window()
_BANKS = _mel_banks()


def _frames(samples: np.ndarray) -> np.ndarray:
    """`snip_edges=False` framing: one frame per hop, centred, edges mirrored.
    `snip_edges=True` would shift every frame by half a window."""
    n = len(samples)
    num_frames = int((n + FRAME_SHIFT // 2) // FRAME_SHIFT)
    first = FRAME_SHIFT // 2 - FRAME_LENGTH // 2  # negative: frame 0 starts before 0
    left = -first
    right = max(0, first + (num_frames - 1) * FRAME_SHIFT + FRAME_LENGTH - n)

    if n > max(left, right):
        # Mirror only the few out-of-range samples, then take a strided view. An
        # index array would be (frames, 400) int64 — 1.15 GB for an hour.
        padded = np.concatenate([samples[:left][::-1], samples, samples[n - right : n][::-1]])
        return sliding_window_view(padded, FRAME_LENGTH)[::FRAME_SHIFT][:num_frames]

    # Shorter than its own padding: a frame overhangs both edges, and Kaldi
    # reflects repeatedly. A 2n-periodic triangle folds as often as needed.
    starts = np.arange(num_frames) * FRAME_SHIFT + first
    idx = (starts[:, None] + np.arange(FRAME_LENGTH)[None, :]) % (2 * n)
    return samples[np.where(idx >= n, 2 * n - idx - 1, idx)]


def fbank(samples: np.ndarray) -> np.ndarray:
    """`(frames, 80)` float32 log-mel energies for 16 kHz mono float samples."""
    if len(samples) < 1:
        return np.zeros((0, NUM_BINS), dtype=np.float32)

    frames = _frames(np.asarray(samples, dtype=np.float64))
    # Kaldi's order: DC offset, preemphasis (first sample against itself),
    # window. Out-of-place: `_frames` returns an overlapping view.
    frames = frames - frames.mean(axis=1, keepdims=True)
    frames[:, 1:] -= PREEMPH * frames[:, :-1]
    frames[:, 0] -= PREEMPH * frames[:, 0]
    frames *= _WINDOW

    spectrum = np.abs(np.fft.rfft(frames, n=FFT_SIZE)) ** 2
    return np.log(np.maximum(spectrum @ _BANKS.T, LOG_FLOOR)).astype(np.float32)
