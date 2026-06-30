"""Noise-robustness sweep: how far do the clean-FLEURS numbers degrade under
noise?

FLEURS is clean read speech, so the routing/recall numbers in `routing.py` are a
best case — real tray audio is far-field and noisy. This degrades each clip with
additive noise at a sweep of SNRs and re-measures, per language:

  - constrained language-detection accuracy (does noise break the routing key?),
  - generalist recall pinned to the true language (transcription degradation).

so you can see WHERE detection and transcription fall apart. White noise (or a
real noise WAV via NOISE_WAV) is a controlled, repeatable proxy for real
conditions — not a substitute for genuine spontaneous/far-field da/no audio,
which is hard to source with exact references. Deterministic: the noise is seeded
per clip, so re-runs reproduce.

  GENERALIST=large-v3-turbo N=10 SNRS=clean,20,10,5  python -m benchmarks.noise_robustness
  NOISE_WAV=/path/to/cafe.wav  python -m benchmarks.noise_robustness   # real babble/room noise
"""

from __future__ import annotations

import os

import numpy as np

from . import _fleurs, _metrics

GENERALIST = os.environ.get("GENERALIST", "large-v3-turbo")
CAND = tuple(os.environ.get("CANDIDATES", "da,no,sv,en").split(","))
N = int(os.environ.get("N", "10"))
SNRS = [s.strip() for s in os.environ.get("SNRS", "clean,20,10,5").split(",")]
NOISE_WAV = os.environ.get("NOISE_WAV", "")


def _noise_source(length: int, seed: int) -> np.ndarray:
    """A unit-RMS noise segment of `length` samples.

    - ``NOISE_WAV`` unset → seeded white Gaussian.
    - ``NOISE_WAV=babble`` → a multi-talker BABBLE bed mixed from several OTHER
      cached FLEURS clips: real speech cross-talk, i.e. the actual interference
      in a multi-person tap (several people talking at once) — the closest
      achievable "real harder audio" given that ungated spontaneous da/no
      corpora are gated or script-based.
    - ``NOISE_WAV=<path>`` → a real noise recording (cafe/room) looped to length.
    """
    if NOISE_WAV == "babble":
        from tapscribe.wav_predecode import load_recorder_wav_as_pcm

        pool = [w for code in _fleurs.CONFIGS for w in sorted((_fleurs.CACHE / code).glob("*.wav"))]
        rng = np.random.default_rng(seed)
        seg = np.zeros(length, dtype=np.float32)
        for i in rng.permutation(len(pool))[:4]:
            clip = load_recorder_wav_as_pcm(pool[i])
            seg += np.tile(clip, int(np.ceil(length / len(clip))))[:length]
    elif NOISE_WAV:
        from tapscribe.wav_predecode import load_recorder_wav_as_pcm

        base = load_recorder_wav_as_pcm(NOISE_WAV)
        seg = np.tile(base, int(np.ceil(length / len(base))))[:length]
    else:
        seg = np.random.default_rng(seed).standard_normal(length).astype(np.float32)
    rms = float(np.sqrt(np.mean(seg**2))) or 1e-9
    return seg / rms


def _add_noise(signal: np.ndarray, snr: str, seed: int) -> np.ndarray:
    if snr == "clean":
        return signal
    s_rms = float(np.sqrt(np.mean(signal**2))) or 1e-9
    target = s_rms / (10.0 ** (float(snr) / 20.0))
    return np.clip(signal + _noise_source(len(signal), seed) * target, -1.0, 1.0)


def main() -> None:
    from faster_whisper import WhisperModel

    from tapscribe.wav_predecode import load_recorder_wav_as_pcm

    gen = WhisperModel(GENERALIST, device="cpu", compute_type="int8")
    kind = f"real noise ({os.path.basename(NOISE_WAV)})" if NOISE_WAV else "white noise"
    print(f"=== noise robustness — {GENERALIST}, {N} clips/lang, {kind}, SNRs={SNRS} ===", flush=True)
    print("(each cell: language-detect accuracy / mean recall)\n", flush=True)
    print("lang   " + "".join(f"{s + 'dB':>16}" for s in SNRS), flush=True)
    for code, (_, true) in _fleurs.CONFIGS.items():
        clips = _fleurs.fetch(code, n=N)
        signals = [load_recorder_wav_as_pcm(w) for w, _ in clips]
        cells = []
        for snr in SNRS:
            det_ok, recalls = 0, []
            for idx, ((_wav, ref), y) in enumerate(zip(clips, signals, strict=True)):
                yn = _add_noise(y, snr, seed=idx)
                _, _, probs = gen.detect_language(yn)
                pm = dict(probs)
                det_ok += max(CAND, key=lambda c: pm.get(c, 0.0)) == true
                # condition_on_previous_text=False stops noise-induced repetition
                # loops from running away (they make Whisper decode unboundedly slow
                # on noisy audio) — also the more realistic setting for short
                # utterances, and what bounds this sweep to a tractable runtime.
                segs, _info = gen.transcribe(yn, language=true, condition_on_previous_text=False)
                text = " ".join(s.text for s in segs)
                recalls.append(_metrics.recall(ref, text))
            cells.append(f"{det_ok}/{len(clips)} r{np.mean(recalls):.2f}")
        print(f"{true:<6} " + "".join(f"{c:>16}" for c in cells), flush=True)


if __name__ == "__main__":
    main()
