# Multi-language quality benchmarks (ADR-0010)

Model-agnostic measurements behind the multi-language design — the yardstick
for any future model. **Not** part of `pytest tests`: they download GB-scale
models plus a speech dataset and (meeting flow) hit an LLM endpoint, so run
them deliberately.

## Data

- **FLEURS** (`google/fleurs`, CC-BY-4.0, ungated) — read-speech clips with
  exact reference transcripts in `da_dk`, `nb_no`, `sv_se`, `en_us`, fetched
  on demand into `~/.cache/tapscribe-fleurs/` (see `_fleurs.py`; needs
  `pip install pyarrow soundfile` — no `datasets` dependency).
- The committed `tests/fixtures/audio/` clips power the lighter, committed
  `test_da_no_routing_benchmark` (see that directory's README).

## Benchmarks

- `python -m benchmarks.routing` — per-language constrained language-detection
  accuracy + per-model recall/WER over N FLEURS clips/language, and the
  generalist-vs-specialist comparison on Norwegian.
  Env: `GENERALIST`, `SPECIALIST`, `N`, `CANDIDATES`.
- `python -m benchmarks.multiperson_meeting` — the multi-person tray meeting
  end-to-end: interleaved single-language utterances (one WAV per utterance)
  through the real pipeline (cover → constrained-detect → specialist routing →
  merge), summarised via an OpenAI-compatible endpoint.
  Env: `OLLAMA_URL`, `SUMMARY_MODEL`, `GENERALIST`.
- `python -m benchmarks.noise_robustness` — degrades each clip with additive
  noise across an SNR sweep and re-measures detect accuracy + generalist
  recall. Seeded white noise by default; point `NOISE_WAV` at a real
  room/babble recording. Env: `GENERALIST`, `N`, `SNRS`, `NOISE_WAV`,
  `CANDIDATES`. Slow with large-v3-turbo on CPU (noisy audio decodes slowly);
  use a smaller `GENERALIST` for a quick shape.

## Recorded baselines

Routing (generalist `large-v3-turbo`, 20 FLEURS clips/language): constrained
language detection **77/77** across da/no/sv/en; **nb-whisper-large** beats the
generalist on Norwegian **19/20 win-or-tie** (recall 0.888 → 0.956, WER
0.096 → 0.056) while nb-medium only tied — hence the default specialist.
ADR-0010 records the decision. Meeting flow: every utterance transcribed in
its own language, routed to the right model, attribution intact, faithful
per-speaker multilingual summary.

Noise (whisper-small, N=4; cell = detect-accuracy · mean recall):

White noise:

| lang | clean | 20 dB | 10 dB | 5 dB |
|---|---|---|---|---|
| da | 4/4 · 0.55 | 4/4 · 0.55 | 4/4 · 0.53 | 4/4 · 0.40 |
| no | 4/4 · 0.70 | 4/4 · 0.67 | 4/4 · 0.53 | 4/4 · 0.38 |
| sv | 4/4 · 0.86 | 4/4 · 0.76 | 4/4 · 0.58 | 4/4 · 0.40 |
| en | 4/4 · 0.94 | 4/4 · 0.89 | 4/4 · 0.84 | 4/4 · 0.76 |

Babble cross-talk (real multi-talker speech — multi-person tap interference):

| lang | clean | 20 dB | 10 dB | 5 dB |
|---|---|---|---|---|
| da | 4/4 · 0.55 | 4/4 · 0.55 | 4/4 · 0.52 | 4/4 · 0.32 |
| no | 4/4 · 0.70 | 4/4 · 0.70 | 4/4 · 0.69 | 4/4 · 0.63 |
| sv | 4/4 · 0.86 | 4/4 · 0.87 | 4/4 · 0.73 | 4/4 · 0.55 |
| en | 4/4 · 0.94 | 4/4 · 0.89 | 4/4 · 0.85 | 3/4 · 0.76 |

Detection — the routing key — survives noise (**16/16 white, 15/16 babble even
at 5 dB**); only recall falls off, sharply at 5 dB. whisper-small keeps CPU
runtime tractable, so these recalls are lower bounds — large-v3-turbo is more
robust (its Danish white-noise row: detect 8/8, recall 0.81 clean → 0.66 at
5 dB, vs small's 0.55 → 0.40).

## Caveats

FLEURS is clean read speech — best-case numbers. Ungated da/no corpora with
references are scarce (Common Voice per-language configs aren't
parquet-exported, NPSC/older Common Voice ship deprecated loading scripts,
VoxPopuli has no da/no), so the harder-audio story is the noise sweep plus the
`NOISE_WAV` hook for any real recording. Mixed languages *within one WAV* (no
silence gap) is uncovered; the honest fix is segmentation/diarization (#78),
not the transcribe or summary layer.
