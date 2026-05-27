#!/usr/bin/env python3
"""Regenerate `tests/fixtures/audio/armstrong-en.wav` from its source.

The fixture must contain Armstrong's iconic line — the exact text in
`armstrong-en.reference.txt`. The source recording, however, opens with a
*different* utterance ("I'm going to step off the LM now"), so a fixed
"first N seconds" trim grabs the wrong sentence (and the live benchmark
then scores good transcripts against a reference the audio never spoke).

This script avoids that by locating the line via word-timestamp
transcription rather than a hardcoded offset: download the source, find
the "small … mankind" span, trim to it (+ padding), write 16 kHz mono
int16 PCM, then re-transcribe the cut so the result is self-verifying.

Run on a box WITH outbound network + faster-whisper (e.g. a dev laptop):

    python tools/recut_armstrong.py

Needs `soundfile` + `scipy` for OGG decode/resample (pip install if
missing) and `faster-whisper` (the `[whisper]` / `[bench]` extra).
"""

from __future__ import annotations

import io
import sys
import urllib.request
import wave
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

SRC = "https://upload.wikimedia.org/wikipedia/commons/d/dd/Armstrong_Small_Step.ogg"
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "audio" / "armstrong-en.wav"
SAMPLE_RATE = 16000
PAD_S = 0.35  # margin each side of the located span so word edges aren't clipped


def load_16k_mono(url: str) -> np.ndarray:
    """Download `url` and return it as a 16 kHz mono float32 waveform."""
    req = urllib.request.Request(url, headers={"User-Agent": "TapScribe-recut/1.0"})
    raw = urllib.request.urlopen(req, timeout=60).read()  # noqa: S310 — fixed https Wikimedia URL
    data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if sr != SAMPLE_RATE:
        g = gcd(int(sr), SAMPLE_RATE)
        mono = resample_poly(mono, SAMPLE_RATE // g, int(sr) // g)
    return mono.astype(np.float32)


def words_with_ts(audio: np.ndarray) -> list[tuple[str, float, float]]:
    """Transcribe `audio` (16 kHz mono float32) into (word, start, end)."""
    from faster_whisper import WhisperModel

    model = WhisperModel("base.en", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(audio, language="en", word_timestamps=True)
    out: list[tuple[str, float, float]] = []
    for seg in segments:
        for w in seg.words or ():
            out.append((w.word.strip().lower().strip(".,!?;:'\""), w.start, w.end))
    return out


def main() -> None:
    print(f"downloading {SRC}")
    audio = load_16k_mono(SRC)
    print(f"source: {len(audio) / SAMPLE_RATE:.1f}s @ {SAMPLE_RATE} Hz mono")

    words = words_with_ts(audio)
    norm = [w for w, _, _ in words]
    print("full transcript:\n  " + " ".join(norm))
    if "small" not in norm or "mankind" not in norm:
        sys.exit("!! 'small'/'mankind' anchors not found — inspect the transcript above and trim by hand")

    s_idx = norm.index("small")
    m_idx = len(norm) - 1 - norm[::-1].index("mankind")
    # Back up two words from "small" to include the leading "that's one".
    start = max(0.0, words[max(0, s_idx - 2)][1] - PAD_S)
    end = words[m_idx][2] + PAD_S
    print(f"trim window: {start:.2f}..{end:.2f}s ({end - start:.2f}s)")

    clip = audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)]
    peak = max(float(np.abs(clip).max()), 1e-9)
    int16 = (clip / peak * 0.9 * 32767).astype(np.int16)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(int16.tobytes())
    print(f"wrote {OUT} ({len(int16) / SAMPLE_RATE:.2f}s)")

    check = words_with_ts(int16.astype(np.float32) / 32767.0)
    print("re-transcribed cut (should match the reference):\n  " + " ".join(w for w, _, _ in check))


if __name__ == "__main__":
    main()
