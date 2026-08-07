# Real-audio fixtures for the E2E pipeline test

`tests/e2e/test_pipeline_e2e.py::test_pipeline_with_real_whisper` streams each
`.wav` here through the `/tap` WebSocket, lets the recorder finalize
per-utterance WAVs, then runs real `faster-whisper` via
`POST /api/transcribe-session`. A fixture passes when at least one ≥ 4-char
reference word appears in the output — strong enough to rule out silence,
hallucination, or a broken bridge; soft enough that tiny/base models don't
flake. Skips when `faster-whisper` isn't installed
(`pip install -e ".[whisper-cpu]"`).

The Danish (`solen-da`) + Norwegian (`marlene-nb`) pair also powers
`test_da_no_routing_benchmark` — the committed, repeatable proof that the
multi-language cover routes the confusable da/no pair (ADR-0010). It reports
per-region, per-model WER + reference word-recall and asserts each region's
winner is the best transcript for its language. Point it at a new model:

```sh
TAPSCRIBE_BENCH_GENERALIST=parakeet-tdt-0.6b-v3 TAPSCRIBE_BENCH_NB=nb-whisper-large \
  pytest tests/e2e/test_pipeline_e2e.py -k da_no_routing_benchmark -m real_audio -s
```

## Adding a fixture

Pair `<name>.wav` with `<name>.reference.txt` containing the **spoken**
transcript (narrations paraphrase written text; WER measures what is actually
said). WAVs must already be in the recorder's wire format (16 kHz mono int16
LE PCM) — the harness refuses anything else so a misconverted fixture fails at
setup, not as garbled transcripts. Convert with either of:

```sh
ffmpeg -i input.ogg -ar 16000 -ac 1 -sample_fmt s16 output.wav
sox input.ogg -r 16000 -c 1 -b 16 output.wav
```

Keep clips short (5–20 s) and redistributable (public domain, CC0, CC-BY /
CC-BY-SA), and record source, licence and attribution below so the repo's
licence record stays honest. Good sources: Wikimedia Commons spoken-Wikipedia
categories (CC-BY-SA/GFDL), LibriVox and LJ Speech (public domain, English),
Mozilla Common Voice nb-NO (CC0).

## Attribution

### `armstrong-en.wav` — 12 s, English, public domain

First ~12 s of
[`Armstrong_Small_Step.ogg`](https://commons.wikimedia.org/wiki/File:Armstrong_Small_Step.ogg)
(NASA recording of Neil Armstrong stepping onto the Moon, 1969), downsampled
from 11 025 Hz mono OGG/Vorbis with `soundfile` + `scipy.signal.resample_poly`.

- **Source**: https://upload.wikimedia.org/wikipedia/commons/d/dd/Armstrong_Small_Step.ogg
- **Licence**: Public domain in the United States as a work of the US federal
  government ("NASA material is not protected by copyright unless noted").
- **Reference**: `"I'm going to step off the LM now."` — what is actually said
  in this clip (the famous "one small step" line comes later, ~15 s into the
  source, and is not in it).

### `marlene-nb.wav` — 15 s, Norwegian Bokmål, CC BY-SA 4.0

15 s window (offset ~9 s, skipping the spoken-Wikipedia preamble) of
[`No-MARLENEDIETRICH.ogg`](https://commons.wikimedia.org/wiki/File:No-MARLENEDIETRICH.ogg),
downsampled from 44 100 Hz stereo OGG/Vorbis.

- **Source**: https://upload.wikimedia.org/wikipedia/commons/0/07/No-MARLENEDIETRICH.ogg
- **Original work**: spoken article "Innlest artikkel om Marlene Dietrich"
  narrated by Elise Øygaren, 23 May 2015; article text from
  [no.wikipedia.org/wiki/Marlene_Dietrich](https://no.wikipedia.org/wiki/Marlene_Dietrich).
- **Licence**: dual GFDL 1.2+ / CC BY-SA 4.0; this snippet is redistributed
  under CC BY-SA 4.0. Attribution: "Elise Øygaren / Wikipedia, CC BY-SA 4.0".
- **Reference**: the spoken article opening (see `marlene-nb.reference.txt`).

### `solen-da.wav` — 15 s, Danish, CC BY-SA 3.0

15 s window (offset ~29 s, skipping the spoken-Wikipedia preamble) of
[`Da-Solen.ogg`](https://commons.wikimedia.org/wiki/File:Da-Solen.ogg),
resampled with `librosa`. Paired with `marlene-nb.wav`, this is the confusable
Bokmål/Danish input the routing benchmark needs.

- **Source**: https://upload.wikimedia.org/wikipedia/commons/2/2e/Da-Solen.ogg
- **Original work**: spoken article "Solen" narrated by *Danielle dk*, version
  of 10 December 2018; article text from
  [da.wikipedia.org/wiki/Solen](https://da.wikipedia.org/wiki/Solen).
- **Licence**: CC BY-SA 3.0; this snippet is redistributed under CC BY-SA 3.0.
  Attribution: "Danielle dk / Wikipedia, CC BY-SA 3.0".
- **Reference**: the spoken article opening, trimmed to the spoken span (see
  `solen-da.reference.txt`).
