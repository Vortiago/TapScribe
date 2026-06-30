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

## Caveats

FLEURS is clean read speech, so these are best-case numbers — real far-field tray
audio will be harder. Mixed languages **within one WAV** (two speakers, no
silence gap) is a rarer edge these don't cover; the honest fix there is
segmentation/diarization (#78), not the transcribe or summary layer.
