# Diarizer reference vectors

`reference.npz` (64 KB) is the numeric oracle for `tapscribe/diarizers/`. Per
audio fixture it holds:

- `fbank__<name>` — the first **100 frames** (1 s at the 10 ms hop) of the
  80-bin log-mel fbank, float32.
- `emb__<name>` — the L2-normalised 512-d speaker embedding, float32.

## Why it is committed

The frontend is part of the model's contract: a subtly wrong window, mel edge
or log floor still yields 512 plausible numbers, so every mocked test passes and
the clustering quietly degrades. Committing the reference means the check runs
in the ordinary suite, not only where the upstream packages happen to be
installed. Same reasoning as `tapscribe/vad/PROVENANCE.md` — a hand port needs
an oracle it cannot drift from.

`tests/test_diarize_fbank.py` checks against this file everywhere;
`tests/test_diarize_fbank_upstream.py` re-derives it live in the
`upstream-contract` CI lane, so the committed copy itself cannot rot unnoticed.

## What produced it

| | |
|---|---|
| fbank | `kaldi-native-fbank` 1.22.3 (Apache-2.0) — the frontend sherpa-onnx feeds these models with, not a reimplementation |
| model | `3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx`, sha256 `357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b` |
| source | `k2-fsa/sherpa-onnx` release `speaker-recongition-models` (upstream's spelling) |
| licence | Apache-2.0 (3D-Speaker, WeSpeaker and sherpa-onnx all verified) — the code/redistribution licence; the weights derive from VoxCeleb |

### Settings the reference pins

```
FbankOptions: dither=0.0, samp_freq=16000, snip_edges=False, num_bins=80
samples scaled to [-1, 1)          # the model's normalize_samples=1
features centred by global-mean CMN before the model                # 3D-Speaker
```

`snip_edges=False` is load-bearing: the `True` variant yields fewer frames *and*
shifts every one of them by half a window, so it changes which audio each frame
describes rather than just how many there are.

## Regenerating

Needs `kaldi-native-fbank` + `onnxruntime` and the model on disk. The generator
is `gen_reference.py` (kept alongside the spike, not shipped) — it reads each
fixture, computes the fbank, keeps the first 100 frames, embeds the full clip,
and writes the archive. Re-running it after a model or upstream bump must leave
`tests/test_diarize_fbank.py` green, or the port needs the matching change.

## onnxruntime miscomputes this model on long inputs (measured)

**`onnxruntime` 1.27.0 returns incoherent embeddings for inputs longer than
~1000 frames (10 s).** Measured on `marlene-nb`, cosine of the clip against its
own first 600 frames:

| frames | 1.27.0 | 1.29.0 |
|---|---|---|
| 1000 | 0.943 | 0.943 |
| 1100 | **0.451** | 0.934 |
| 1234 | **0.254** | 0.933 |
| 1400 | 0.931 | 0.931 |
| 1500 | **0.549** | 0.925 |

Discrimination goes with it: at 1500 frames `marlene-nb` vs `solen-da` reads
**0.566** on 1.27 against 0.057 on 1.29 — under 1.27 a speaker resembles a
stranger more than herself, which is clustering noise, not a threshold to tune.
1.29 degrades smoothly with length, which is what a correct model does.

Two consequences the engine must respect:

1. **Embed in bounded windows and average**, never one pass over a whole tap.
   That is the right shape anyway — speaker embedding is a short-window
   operation — and it keeps every call far below the threshold.
2. **The failure is silent.** Nothing raises; the vectors stay unit-norm and
   plausible. So a runtime that fails self-consistency has to fail a test
   instead, or a bad `onnxruntime` degrades diarization with no signal.

`pyproject.toml` pins `onnxruntime>=1.17` unbounded. The VAD is unaffected —
Silero is fed 512-sample windows, orders of magnitude below where this appears.
