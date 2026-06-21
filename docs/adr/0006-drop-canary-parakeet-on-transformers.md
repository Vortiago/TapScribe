---
status: accepted
date: 2026-06-21
---

# Drop Canary; move Parakeet CUDA/CPU off NeMo to `transformers`

## Context

ADR-0003 introduced two NeMo-backed families: Parakeet and Canary, each
with an MLX adapter (Apple Silicon) and a NeMo adapter (CUDA/CPU). NeMo
(`nemo_toolkit[asr]`) turned out to be the single heaviest and most
install-fragile dependency in the tree:

- **NeMo's only runtime consumers were `parakeet.py` and `canary.py`.**
  Everything else (Whisper, NB-Whisper, Voxtral, the MLX adapters) is
  NeMo-free.
- **The `kaldialign` tar pit.** NeMo 2.6 through 2.7.3 (latest) all pin
  `kaldialign<=0.9.1` under the `asr` extra, and `kaldialign 0.9.1` has
  no cp313 macOS wheel — forcing a source build that fails on a stock
  Mac mini. We worked around it by capping macOS NeMo to `<2.6` and
  pinning `kaldialign>=0.9.2,<0.10` (the wheel-available window). A
  Dependabot PR (#116) trying to widen that cap kept tripping the guard
  test; the cap could not be lifted because the upstream pin persists.
- **Canary is NeMo-only.** `transformers` doesn't support Canary
  (upstream feature request open); `mlx-audio` is Metal-only — and the
  repo's `mlx_canary.py` / `_load_canary_mlx` was dead code, never wired
  into `_CANARY_BACKENDS`. So Canary was, in practice, NeMo-only and
  CUDA/CPU-only.
- **`transformers` v5 ships native Parakeet.** `ParakeetForTDT` /
  `AutoModelForTDT` + `AutoProcessor` landed in a stable release. The
  lower-level `model.generate(..., return_dict_in_generate=True)` +
  `processor.decode(sequences, durations=...)` path yields per-token
  timestamps; our `wav_predecode` float32 array feeds the processor
  directly (still ffmpeg-free). Verified end-to-end on CPU before
  committing to this ADR.

Canary's only differentiating feature was X↔English **translation**.
The operator confirmed it was never used.

## Decision

Remove NeMo entirely, in one change:

1. **Drop the Canary family.** Delete `canary.py`, the dead
   `mlx_canary.py`, the catalog backends/model entry, the `canary-cpu`
   and `canary` extras, the install-picker family, and their tests.
   TapScribe no longer offers speech translation.
2. **Reimplement `ParakeetTranscriber` on `transformers`.** Same
   `name="parakeet"`, `backend="parakeet-hf"`. Token timestamps are
   folded into word/segment alignment by
   `_parakeet_tdt.build_segments_from_tdt_tokens` (a token begins a new
   word iff its text carries a leading space; punctuation/continuations
   attach to the current word; segments break on sentence-final punct).
   Long audio is chunked through the shared `chunking.chunk_windows`,
   the same as the MLX adapter.
3. **`parakeet-cpu` extra becomes `transformers>=5.12,<6` + `librosa` +
   `torch`** — no platform split, no `kaldialign`, no `nemo_toolkit`.
   The macOS NeMo `<2.6` / kaldialign wheel-window dance is retired, and
   so is its guard test.
4. **Keep `mlx_parakeet`** as the Apple-Silicon path. The `parakeet`
   alias still steers Apple Silicon to MLX and everywhere else to the
   `transformers` backend.

`_nemo_payload.py` is deleted (its only consumers were the two NeMo
adapters). An `importorskip`-gated upstream-contract smoke test
(`test_transformers_parakeet_upstream_contract`) replaces the deleted
mlx-audio Canary contract test and pins the `transformers` symbols +
`processor.decode(durations=...)` signature.

## Consequences

- **NeMo and kaldialign are gone.** No more PyTorch-Lightning / Hydra,
  no more macOS source-build failures, no more version-cap gymnastics.
  Dependabot PR #116 is moot — the dependency it tried to bump no longer
  exists.
- **macOS now runs a current Parakeet stack** on CUDA/CPU (it was frozen
  at NeMo 2.5.x). `transformers` and `librosa` have wheels everywhere.
- **Lost capability: speech translation.** Canary was the only
  translating backend. The `source_language` / `target_language` result
  fields, the `SelectInput` type, and the dashboard's translation badge
  are retained (back-compat with any sidecar cached from a Canary run,
  and for a future translating adapter) but no shipped adapter populates
  them today.
- **New caveat: `transformers` Parakeet timestamps are token-level.**
  The ASR *pipeline*'s `return_timestamps="word"` is CTC-only and raises
  on a TDT transducer, so we use the lower-level generate/decode path and
  reconstruct words/segments ourselves.

This supersedes the Canary-specific portions of ADR-0003 (the Parakeet
registry design otherwise stands).

## Alternatives considered

- **Keep Canary, only migrate Parakeet.** Leaves NeMo + kaldialign in
  the tree (Canary needs them), so it gains little. Rejected: the win is
  removing NeMo, which requires dropping Canary.
- **Just bump the kaldialign cap (Dependabot #116).** Doesn't address
  NeMo's weight or the recurring macOS install fragility; only delays the
  next cap fight. Rejected once the `transformers` Parakeet path was
  proven.
- **Run Canary without NeMo.** Not possible today: `transformers` has no
  Canary support and `mlx-audio` is Metal-only.
