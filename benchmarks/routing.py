"""Multi-language routing benchmark (ADR-0010): does the cover route da/no/sv/en
correctly, and does the specialist earn its place?

Over N FLEURS clips per language it measures, for each clip:
  - the generalist's CONSTRAINED language detection (is the true language picked
    out of the declared set?), and
  - per-model word-recall + WER vs the exact reference,
then aggregates per language and reports whether the Norwegian specialist beats
the generalist (the question behind nb-whisper-medium vs -large).

Models + sample size are env-tunable so this runs against any future model:
  GENERALIST=large-v3-turbo  SPECIALIST=nb-whisper-large  N=20  \
    python -m benchmarks.routing

Needs faster-whisper + the FLEURS cache (fetched on demand). Best run with
HF_HUB_OFFLINE=1 once the model weights are cached locally.
"""

from __future__ import annotations

import os
import wave

import numpy as np

from . import _fleurs, _metrics

CAND = tuple(os.environ.get("CANDIDATES", "da,no,sv,en").split(","))
GENERALIST = os.environ.get("GENERALIST", "large-v3-turbo")
SPECIALIST = os.environ.get("SPECIALIST", "nb-whisper-large")
N = int(os.environ.get("N", "20"))


def _read(path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def main() -> None:
    from faster_whisper import WhisperModel

    from tapscribe.transcribers.faster_whisper import FasterWhisperTranscriber

    gen = WhisperModel(GENERALIST, device="cpu", compute_type="int8")
    print(f"=== detect accuracy ({GENERALIST}, constrained to {CAND}) ===", flush=True)
    clips: dict[str, list] = {}
    for code, (_, true) in _fleurs.CONFIGS.items():
        pairs = _fleurs.fetch(code, n=N)
        clips[code] = [(w, r, true) for w, r in pairs]
        ok = 0
        for wav, _ref, _t in clips[code]:
            _, _, probs = gen.detect_language(_read(wav))
            pm = dict(probs)
            det = max(CAND, key=lambda c: pm.get(c, 0.0))
            ok += det == true
        print(f"  {true:<3} detect_acc={ok}/{len(clips[code])}", flush=True)

    # Generalist vs the Norwegian specialist on Norwegian clips.
    spec = FasterWhisperTranscriber.load(SPECIALIST)
    print(f"\n=== Norwegian: {GENERALIST} vs {SPECIALIST} (recall / wer) ===", flush=True)
    gr, sr, gw, sw, wins = [], [], [], [], 0
    for wav, ref, _t in clips["nb"]:
        y = _read(wav)
        gt = " ".join(s.text.strip() for s in gen.transcribe(y, language="no")[0])
        st = " ".join(s.text for s in spec._model.transcribe(y, language="no")[0])
        a, b = _metrics.recall(ref, gt), _metrics.recall(ref, st)
        gr.append(a)
        sr.append(b)
        gw.append(_metrics.wer(ref, gt))
        sw.append(_metrics.wer(ref, st))
        wins += b > a
        verdict = "SPEC" if b > a else ("=" if b == a else "gen")
        print(
            f"  {wav.stem[:14]:<14} gen {a:.2f}/{gw[-1]:.2f}  spec {b:.2f}/{sw[-1]:.2f}  {verdict}",
            flush=True,
        )
    print(
        f"\n  MEAN gen recall={np.mean(gr):.3f} wer={np.mean(gw):.3f} | "
        f"spec recall={np.mean(sr):.3f} wer={np.mean(sw):.3f} | spec beats gen {wins}/{len(gr)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
