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

## Layout

```
tests/fixtures/audio/
├── armstrong-en.wav            # 12 s, 16 kHz mono int16, NASA PD
├── armstrong-en.reference.txt
├── marlene-nb.wav              # 15 s, 16 kHz mono int16, CC-BY-SA 4.0
├── marlene-nb.reference.txt
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
- **Reference transcript**: `"That's one small step for man, one giant
  leap for mankind."` — the well-known phrase Armstrong utters in the
  clip.

### `marlene-nb.wav`

First ~15 seconds of [`No-MARLENEDIETRICH.ogg`](https://commons.wikimedia.org/wiki/File:No-MARLENEDIETRICH.ogg),
a spoken-Wikipedia reading of the Norwegian Wikipedia article on
Marlene Dietrich, downsampled from 44 100 Hz stereo OGG/Vorbis to
16 kHz mono int16 WAV.

- **Source**: https://upload.wikimedia.org/wikipedia/commons/0/07/No-MARLENEDIETRICH.ogg
- **Original work**: Spoken article "Innlest artikkel om Marlene
  Dietrich" narrated by Elise Øygaren, 23 May 2015. Article text from
  [no.wikipedia.org/wiki/Marlene_Dietrich](https://no.wikipedia.org/wiki/Marlene_Dietrich).
- **Licence**: Dual-licensed under GFDL 1.2+ and Creative Commons
  Attribution-ShareAlike 4.0 International (this snippet is
  redistributed under CC-BY-SA 4.0). Attribution: "Elise Øygaren /
  Wikipedia, CC BY-SA 4.0".
- **Reference transcript**: the article's opening sentence —
  `"Marlene Dietrich, egentlig Maria Magdalene Dietrich, født 27.
  desember 1901 i Berlin, død 6. mai 1992 i Paris, var en tyskfødt
  amerikansk skuespillerinne og sangerinne."`

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
