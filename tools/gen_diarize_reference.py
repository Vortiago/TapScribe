#!/usr/bin/env python3
"""Regenerate `tests/fixtures/diarize/reference.npz` — the diarizer's oracle.

ONE piece of code produces the oracle; `tests/test_diarize_fbank.py` checks the
numpy port against it everywhere, and `tests/test_diarize_fbank_upstream.py`
re-derives it live in the `upstream-contract` CI lane so the committed copy
cannot rot unnoticed.

Needs the two upstream packages the shipped code deliberately does NOT depend
on, plus the embedding model (fetched at bring-up, not vendored):

    pip install kaldi-native-fbank
    python3 -m tapscribe.diarizers.model      # if it isn't fetched yet
    python3 tools/gen_diarize_reference.py

Re-running after a model or upstream bump must leave `test_diarize_fbank.py`
green, or the port needs the matching change.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "tests" / "fixtures" / "diarize" / "reference.npz"
AUDIO = REPO_ROOT / "tests" / "fixtures" / "audio"

FIXTURES = ("armstrong-en", "marlene-nb", "solen-da")
#: Frames kept per fixture at each end. The HEAD pins the ordinary path; the
#: TAIL pins the right-edge mirror, which `snip_edges=False` framing depends on
#: and which a head-only slice never compares.
KEEP_FRAMES = 100
#: A short signal exercises repeated reflection — a frame overhanging BOTH edges
#: at once. Deterministic so the archive is reproducible.
SHORT_LEN = 120


def fbank_reference(samples: np.ndarray) -> np.ndarray:
    """kaldi-native-fbank with the settings the embedding model was trained on."""
    import kaldi_native_fbank as knf

    opts = knf.FbankOptions()
    opts.frame_opts.dither = 0.0
    opts.frame_opts.samp_freq = 16000
    opts.frame_opts.snip_edges = False
    opts.mel_opts.num_bins = 80
    f = knf.OnlineFbank(opts)
    f.accept_waveform(16000, samples.tolist())
    f.input_finished()
    if f.num_frames_ready == 0:
        return np.zeros((0, 80), dtype=np.float32)
    return np.stack([f.get_frame(i) for i in range(f.num_frames_ready)]).astype(np.float32)


def short_signal() -> np.ndarray:
    """A fixed short waveform — under one frame length, so it folds twice."""
    return (np.random.default_rng(7).standard_normal(SHORT_LEN) * 0.05).astype(np.float32)


def build(model: Path | None) -> dict[str, np.ndarray]:
    from tapscribe.wav_predecode import load_recorder_wav_as_pcm

    payload: dict[str, np.ndarray] = {"fbank__short": fbank_reference(short_signal())}
    session = None
    if model is not None:
        import onnxruntime as ort

        session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])

    for name in FIXTURES:
        feats = fbank_reference(load_recorder_wav_as_pcm(AUDIO / f"{name}.wav"))
        payload[f"fbank__{name}"] = feats[:KEEP_FRAMES]
        payload[f"fbank_tail__{name}"] = feats[-KEEP_FRAMES:]
        if session is not None:
            centred = feats - feats.mean(axis=0, keepdims=True)  # global-mean CMN
            vec = session.run(["embedding"], {"x": centred[None, :, :]})[0][0].astype(np.float64)
            payload[f"emb__{name}"] = (vec / np.linalg.norm(vec)).astype(np.float32)
        print(f"{name}: {feats.shape} -> head/tail {KEEP_FRAMES}")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--model",
        type=Path,
        default=None,
        help="override the fetched speaker-embedding ONNX.",
    )
    ap.add_argument(
        "--fbank-only",
        action="store_true",
        help="skip the embeddings, so no model is needed.",
    )
    args = ap.parse_args()

    sys.path.insert(0, str(REPO_ROOT))
    from tapscribe.diarizers import model as diarize_model

    model = None if args.fbank_only else (args.model or diarize_model.model_path())
    payload = build(model)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT, **payload)
    print(f"\nwrote {OUT.relative_to(REPO_ROOT)} ({OUT.stat().st_size / 1024:.1f} KB)")
    if model is not None:
        print("model sha256:", hashlib.sha256(model.read_bytes()).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
