---
status: accepted
date: 2026-06-21
---

# Drop Canary; move Parakeet CUDA/CPU off NeMo to `transformers`

## Decision

NeMo (`nemo_toolkit[asr]`) is out of the tree entirely:

- **No Canary family** (`canary.py`, the dead `mlx_canary.py`, its
  catalog entries, extras, and install-picker family are deleted).
  TapScribe no longer offers speech translation — X↔English translation
  was Canary's only differentiating feature, and the operator never
  used it.
- **`ParakeetTranscriber` (CUDA/CPU) runs on `transformers`** — same
  `name="parakeet"`, `backend="parakeet-hf"`. The lower-level
  `model.generate(..., return_dict_in_generate=True)` +
  `processor.decode(sequences, durations=...)` path yields per-token
  timestamps, folded into word/segment alignment by
  `_parakeet_tdt.build_segments_from_tdt_tokens`; long audio chunks
  through the shared `chunking.chunk_windows`, like the MLX adapter;
  the `wav_predecode` float32 array feeds the processor directly
  (still ffmpeg-free).
- **The `parakeet-cpu` extra is `transformers>=5.12,<6` + `librosa` +
  `torch`** — no platform split, no `kaldialign`, no `nemo_toolkit`.
- **`mlx_parakeet` stays** the Apple-Silicon path; the `parakeet` alias
  steers Apple Silicon to MLX, everywhere else to `parakeet-hf`.
- `test_transformers_parakeet_upstream_contract` (importorskip-gated)
  pins the `transformers` symbols + `decode(durations=...)` signature.

## Why

NeMo was the single heaviest and most install-fragile dependency in the
tree, and its only runtime consumers were `parakeet.py` and `canary.py`.
Its `asr` extra pins `kaldialign<=0.9.1`, which has no cp313 macOS wheel
— forcing a macOS NeMo `<2.6` cap plus a kaldialign wheel-window pin the
upstream pin would never let us lift. `transformers` v5 ships native Parakeet
(`ParakeetForTDT` / `AutoModelForTDT` + `AutoProcessor`), verified
end-to-end on CPU. Canary cannot follow: `transformers` has no Canary
support, `mlx-audio` is Metal-only, and the repo's `mlx_canary.py` was
dead code — removing NeMo requires dropping Canary.

## Consequences

- No PyTorch-Lightning / Hydra, no macOS source-build failures, no
  version-cap gymnastics; macOS runs a current Parakeet stack on
  CUDA/CPU. `transformers` and `librosa` have wheels everywhere.
- **Lost capability: speech translation.** *Update 2026-07-08:* the
  Canary vestiges are deleted — the `target_lang` kwarg /
  `target_language` field thread, the `SelectInput` type, and the
  dashboard's translation badge are gone (`source_language` stays; it
  carries the ADR-0010 language pin). Canary-era sidecars still load;
  their `target_language` key is ignored, so a Canary-translated
  sidecar's text renders with no translation cue. A future translating
  adapter re-adds the thread in its own PR.
- **`transformers` Parakeet timestamps are token-level.** The ASR
  *pipeline*'s `return_timestamps="word"` is CTC-only and raises on a
  TDT transducer, so we use the lower-level generate/decode path and
  reconstruct words/segments ourselves.

Supersedes the Canary-specific portions of ADR-0003 (the Parakeet
registry design otherwise stands).

## Alternatives considered

- **Keep Canary, migrate only Parakeet** — leaves NeMo + kaldialign in
  the tree (Canary needs them); the win is removing NeMo.
- **Just bump the kaldialign cap (Dependabot #116)** — delays the next
  cap fight without addressing NeMo's weight or macOS fragility.
- **Run Canary without NeMo** — not possible: no `transformers`
  support, and `mlx-audio` is Metal-only.
