# Diarizer reference vectors

`reference.npz` (64 KB) is the numeric oracle for `tapscribe/diarizers/`. Per
audio fixture it holds:

- `fbank__<name>` / `fbank_tail__<name>` — the first and last **100 frames**
  of the 80-bin log-mel fbank, float32. Both ends, because `snip_edges=False`
  mirrors at each and a head-only slice never compares the right edge.
- `fbank__short` — a 120-sample signal, shorter than its own padding, so the
  repeated reflection is covered.
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

```bash
pip install kaldi-native-fbank onnxruntime
python3 tools/gen_diarize_reference.py --model /path/to/campplus.onnx
```

Omit `--model` to refresh the fbank references only. Must leave
`tests/test_diarize_fbank.py` green afterwards, or the port needs the matching
change.

**Generate on a runtime that passes the coherence check below** — the
embeddings were once regenerated on 1.27.0 and silently encoded its bug, which
made the comparison test pass for the wrong reason.

## onnxruntime miscomputes this model on long inputs (measured)

**`onnxruntime` 1.27.0 and 1.28.0 return incoherent embeddings for inputs
longer than ~1000 frames (10 s); 1.29.0 is the fix.** Measured on `marlene-nb`,
cosine of the clip against its own first 600 frames (1.27 and 1.28 agree to
every digit shown):

| frames | 1.27.0 / 1.28.0 | 1.29.0 |
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
   plausible. `test_the_runtime_embeds_long_input_coherently` is what turns it
   into a failure — it embeds 1500 frames and asserts a speaker still resembles
   herself. On 1.27.0 it fails at 0.549.

`pyproject.toml` therefore floors `onnxruntime>=1.29`. The VAD does not need
it, but one dependency gets one stated requirement — a second floor hidden in a
consumer would be a shadow source of truth. No platform cost: macOS has been
arm64-only since 1.26, and `>=1.17` was already fictional on py3.13/3.14, which
have no 1.17 wheels.
