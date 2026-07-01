# Multi-language quality benchmarks (ADR-0010)

Reproducible, model-agnostic measurements behind the multi-language
transcription design. **Not** part of `pytest tests` — they download GB-scale
models + a speech dataset and (for the meeting flow) hit an LLM endpoint, so you
run them deliberately. They are the yardstick for every future model.

## Data

- **FLEURS** (`google/fleurs`, CC-BY-4.0, ungated) — short read-speech clips with
  **exact** reference transcripts in `da_dk`, `nb_no`, `sv_se`, `en_us`. Fetched
  on demand into `~/.cache/tapscribe-fleurs/` (not committed; see `_fleurs.py`).
  Needs `pip install pyarrow soundfile` (pyarrow rides in with `datasets`).
- The committed `tests/fixtures/audio/` clips (`solen-da`, `marlene-nb`,
  `armstrong-en`) power the lighter, committed `test_da_no_routing_benchmark`.

## Benchmarks

### `python -m benchmarks.routing`
Per-language **constrained language-detection accuracy** + per-model **recall/WER**
over N FLEURS clips/language, and the **generalist-vs-specialist** comparison on
Norwegian. Env: `GENERALIST`, `SPECIALIST`, `N`, `CANDIDATES`.

### `python -m benchmarks.multiperson_meeting`
The realistic **multi-person tray meeting** end-to-end: interleaved
single-language utterances (the silence-split = one WAV per utterance) through
the real pipeline (cover → constrained-detect → specialist-routing → merge),
summarised via an OpenAI-compatible endpoint. Env: `OLLAMA_URL`, `SUMMARY_MODEL`,
`GENERALIST`.

### `python -m benchmarks.noise_robustness`
**How far do the clean numbers degrade under noise?** FLEURS is clean read
speech, so routing.py's numbers are best-case. This degrades each clip with
additive noise at a sweep of SNRs and re-measures, per language, language-detect
accuracy + generalist recall — so you see where detection and transcription fall
apart. White noise (seeded, repeatable) by default; point `NOISE_WAV` at a real
room/babble recording for a realistic far-field proxy. Env: `GENERALIST`, `N`,
`SNRS`, `NOISE_WAV`. (Slow with large-v3-turbo on CPU — noisy audio decodes
slowly — so it's a deliberate run; use a smaller `GENERALIST` for a quick shape.)

## Results (generalist = `large-v3-turbo`, 20 clips/language, FLEURS test)

**Constrained language detection — 77/77 correct** across the confusable
Scandinavian trio + English. The pin/detect (slice 1) is the load-bearing,
validated mechanism:

| | da | no | sv | en |
|---|---|---|---|---|
| detect_acc | 19/19 | 20/20 | 19/19 | 19/19 |

**Specialist (Norwegian), generalist vs nb-whisper:**

| specialist | beats generalist | mean recall (gen → spec) | mean WER (gen → spec) |
|---|---|---|---|
| nb-whisper-**medium** | ~tied | 0.93 → 0.96 | — |
| nb-whisper-**large** | **19/20 win-or-tie** (10 win, 9 tie, 1 loss) | 0.888 → **0.956** | 0.096 → **0.056** |

→ the default specialist is **nb-whisper-large**: medium didn't earn its extra
decode, large does (+0.07 recall, ~40% lower WER).

**Multi-person meeting (3 speakers, da/no/en):** every utterance transcribed in
its own language and routed to the right model (Danish/English → generalist,
Norwegian → nb-whisper), speaker attribution intact, and a faithful per-speaker
multilingual summary. The realistic per-utterance flow works end-to-end.

### Noise robustness (whisper-small, N=4; cell = detect-accuracy · mean recall)

*Detection — the routing key — is the robust part; recall degrades gracefully.*

White noise:

| lang | clean | 20 dB | 10 dB | 5 dB |
|---|---|---|---|---|
| da | 4/4 · 0.55 | 4/4 · 0.55 | 4/4 · 0.53 | 4/4 · 0.40 |
| no | 4/4 · 0.70 | 4/4 · 0.67 | 4/4 · 0.53 | 4/4 · 0.38 |
| sv | 4/4 · 0.86 | 4/4 · 0.76 | 4/4 · 0.58 | 4/4 · 0.40 |
| en | 4/4 · 0.94 | 4/4 · 0.89 | 4/4 · 0.84 | 4/4 · 0.76 |

Babble cross-talk (real multi-talker speech — the multi-person tap interference):

| lang | clean | 20 dB | 10 dB | 5 dB |
|---|---|---|---|---|
| da | 4/4 · 0.55 | 4/4 · 0.55 | 4/4 · 0.52 | 4/4 · 0.32 |
| no | 4/4 · 0.70 | 4/4 · 0.70 | 4/4 · 0.69 | 4/4 · 0.63 |
| sv | 4/4 · 0.86 | 4/4 · 0.87 | 4/4 · 0.73 | 4/4 · 0.55 |
| en | 4/4 · 0.94 | 4/4 · 0.89 | 4/4 · 0.85 | 3/4 · 0.76 |

**Language detection survives noise: 16/16 (white) and 15/16 (babble) correct even
at 5 dB SNR** across the confusable Scandinavian trio + English — so the routing
foundation holds under harder audio; only transcription recall falls off (sharply
at 5 dB). whisper-**small** is used for tractable CPU runtime, so these absolute
recalls are LOWER bounds — production **large-v3-turbo is more robust** (its
Danish white-noise row: detect 8/8, recall **0.81** clean → **0.66** at 5 dB, vs
small's 0.55 → 0.40). The full large-v3-turbo table is a deliberate run (it
decodes noisy audio slowly on CPU).

## Caveats

FLEURS is clean read speech, so the routing/recall numbers above are best-case.
On **genuinely harder _real_ audio**: ungated da/no SPEECH corpora with
references are scarce — Common Voice's per-language configs aren't parquet-
exported, NPSC and older Common Voice ship deprecated loading scripts (modern
`datasets` won't load them), and VoxPopuli has no da/no. So the harder-audio
story rests on the sweep above — white noise, **babble cross-talk** (real
multi-talker speech, the actual multi-person interference), and the
`NOISE_WAV=<path>` hook for any real room/meeting recording the operator supplies.
Mixed languages **within one WAV** (two speakers, no silence gap) is a rarer edge
none of these cover; the honest fix there is segmentation/diarization (#78), not
the transcribe or summary layer.
