# Real-audio fixtures for the E2E pipeline test

`tests/e2e/test_pipeline_e2e.py::test_pipeline_with_real_whisper` streams
each `.wav` here through the `/tap` WebSocket bridge, lets the recorder
finalize a per-utterance WAV, then triggers `POST /api/transcribe-session`
to run `faster-whisper` on what the bridge delivered. The test passes
when at least one ≥ 4-char word from the reference transcript shows up
in the model's output for each fixture — strong enough to rule out
silence, hallucination, or a broken bridge, soft enough that tiny/base
Whisper models don't make it flake.

Skipped automatically when `faster-whisper` isn't installed
(`pip install -e ".[whisper]"`).

## da/no routing benchmark

The Danish (`solen-da`) + Norwegian (`marlene-nb`) pair powers
`test_da_no_routing_benchmark` — the measurable, repeatable proof that the
multi-language cover routes the confusable da/no pair correctly (ADR-0010). It
streams both as one meeting, runs the real cover, and reports per-region,
per-model **WER + reference word-recall** plus which transcript the selector
chose, asserting each region's winner is the best transcript for its language.
Point it at a new model and re-read the numbers:

```sh
TAPSCRIBE_BENCH_GENERALIST=parakeet-tdt-0.6b-v3 TAPSCRIBE_BENCH_NB=nb-whisper-large \
  pytest tests/e2e/test_pipeline_e2e.py -k da_no_routing_benchmark -m real_audio -s
```

Both `-da`/`-nb` references are the **spoken** article text (the narration
paraphrases the written article slightly, and the spoken-Wikipedia preamble is
skipped), so WER measures transcription quality against what is actually said.

## Layout

```
tests/fixtures/audio/
├── armstrong-en.wav            # 12 s, 16 kHz mono int16, NASA PD
├── armstrong-en.reference.txt
├── marlene-nb.wav              # 15 s, 16 kHz mono int16, CC-BY-SA 4.0
├── marlene-nb.reference.txt
├── solen-da.wav                # 15 s, 16 kHz mono int16, CC-BY-SA 3.0
├── solen-da.reference.txt
└── README.md (this file)
```

Each `<name>.wav` must be paired with a `<name>.reference.txt`
containing the spoken transcript. WAVs are required to already be in
the recorder's wire format (16 kHz mono int16 LE PCM); the harness will
refuse anything else so a misconverted fixture fails at test setup
rather than producing garbled transcripts later.

## Attribution

### `armstrong-en.wav`

First ~12 seconds of [`Armstrong_Small_Step.ogg`](https://commons.wikimedia.org/wiki/File:Armstrong_Small_Step.ogg)
from Wikimedia Commons, downsampled from 11 025 Hz mono OGG/Vorbis to
16 kHz mono int16 WAV with `soundfile` + `scipy.signal.resample_poly`.

- **Source**: https://upload.wikimedia.org/wikipedia/commons/d/dd/Armstrong_Small_Step.ogg
- **Original work**: NASA recording of Neil Armstrong stepping onto
  the Moon, 1969.
- **Licence**: Public domain in the United States as a work of the US
  federal government ("NASA material is not protected by copyright
  unless noted").
- **Reference transcript**: `"I'm going to step off the LM now."` — what
  Armstrong actually says in the **first ~12 s** of the recording (the
  famous "one small step for man" line comes *later*, at ~15 s in the
  source, and is not in this clip).

### `marlene-nb.wav`

A 15 s window (offset ~9 s, **skipping the spoken-Wikipedia preamble**)
of [`No-MARLENEDIETRICH.ogg`](https://commons.wikimedia.org/wiki/File:No-MARLENEDIETRICH.ogg),
a spoken-Wikipedia reading of the Norwegian Wikipedia article on
Marlene Dietrich, downsampled from 44 100 Hz stereo OGG/Vorbis to
16 kHz mono int16 WAV. The reference is the **spoken** article opening
(the narration paraphrases the written text slightly), so WER/recall
measure transcription quality against what is actually said.

- **Source**: https://upload.wikimedia.org/wikipedia/commons/0/07/No-MARLENEDIETRICH.ogg
- **Original work**: Spoken article "Innlest artikkel om Marlene
  Dietrich" narrated by Elise Øygaren, 23 May 2015. Article text from
  [no.wikipedia.org/wiki/Marlene_Dietrich](https://no.wikipedia.org/wiki/Marlene_Dietrich).
- **Licence**: Dual-licensed under GFDL 1.2+ and Creative Commons
  Attribution-ShareAlike 4.0 International (this snippet is
  redistributed under CC-BY-SA 4.0). Attribution: "Elise Øygaren /
  Wikipedia, CC BY-SA 4.0".
- **Reference transcript**: the spoken article opening —
  `"Marlene Dietrich, egentlig Maria Magdalena Dietrich, ble født den
  27. desember 1901 i Berlin og døde 6. mai 1992 i Paris. Hun var en
  tysk-amerikansk skuespillerinne."`

### `solen-da.wav`

A 15 s window (offset ~29 s, **skipping the spoken-Wikipedia preamble**)
of [`Da-Solen.ogg`](https://commons.wikimedia.org/wiki/File:Da-Solen.ogg),
a spoken-Wikipedia reading of the Danish Wikipedia article on the Sun,
resampled to 16 kHz mono int16 WAV with `librosa`. Paired with the
Norwegian `marlene-nb.wav`, this is the **Danish + Norwegian** input the
`da/no` routing benchmark needs — the confusable Bokmål/Danish pair the
multi-language cover exists to disambiguate.

- **Source**: https://upload.wikimedia.org/wikipedia/commons/2/2e/Da-Solen.ogg
- **Original work**: Spoken article "Solen" narrated by *Danielle dk*,
  version of 10 December 2018. Article text from
  [da.wikipedia.org/wiki/Solen](https://da.wikipedia.org/wiki/Solen).
- **Licence**: Creative Commons Attribution-ShareAlike 3.0 (this snippet
  is redistributed under CC-BY-SA 3.0). Attribution: "Danielle dk /
  Wikipedia, CC BY-SA 3.0".
- **Reference transcript**: the article's opening, trimmed to the spoken
  span — `"Solen, latin Sol, græsk Helios, er den stjerne, som sammen med
  sit planetsystem udgør solsystemet. Jorden og andet stof, herunder
  andre planeter, asteroider."`

## Adding more fixtures

Drop another `<name>.wav` + `<name>.reference.txt` pair in this
directory. The recorder's wire format is 16 kHz mono int16 LE PCM;
convert with `soundfile`:

```py
import wave, numpy as np, soundfile as sf
from scipy.signal import resample_poly
from math import gcd

data, sr = sf.read("input.ogg", dtype="float32", always_2d=True)
mono = data.mean(axis=1)
g = gcd(int(sr), 16000); resampled = resample_poly(mono, 16000 // g, int(sr) // g)
int16 = (resampled / max(abs(resampled).max(), 1e-9) * 0.9 * 32767).astype(np.int16)
with wave.open("output.wav", "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(int16.tobytes())
```

Equivalent one-liners with ffmpeg or sox if you prefer:

```sh
ffmpeg -i input.ogg -ar 16000 -ac 1 -sample_fmt s16 output.wav
sox input.ogg -r 16000 -c 1 -b 16 output.wav
```

Keep fixtures short (a few seconds — 5–20 s is a good range) and pick
sources licensed for redistribution: public domain, CC0, or CC-BY /
CC-BY-SA with the attribution recorded above. List the new clip in
**Attribution** when you add it so the repo's licence record stays
honest.

## Suggested sources

- **English**: Wikimedia Commons audio (many CC-BY-SA),
  [LJ Speech Dataset](https://keithito.com/LJ-Speech-Dataset/) (public
  domain), [LibriVox](https://librivox.org/) (public domain audiobook
  recordings of Gutenberg texts).
- **Norwegian Bokmål**: [Mozilla Common Voice — nb-NO](https://commonvoice.mozilla.org/nb-NO/datasets)
  (CC0), Wikimedia Commons spoken Wikipedia articles
  ([Category:Spoken Norwegian (Bokmål) Wikipedia](https://commons.wikimedia.org/wiki/Category:Spoken_Norwegian_(Bokm%C3%A5l)_Wikipedia),
  GFDL / CC-BY-SA).
