# Live-transcription tuning: research notes

Background for the live-quality investigation. The **live** path is not
the batch path — it runs WhisperLiveKit (WlK) as a subprocess, fed by
TapScribe's own Silero `SpeechGate`, with captions surfaced through
`WlKRelay`. This note records what the two upstreams recommend, how our
current defaults compare, and the concrete hypotheses the benchmark
(`tools/bench_live.py`) should confirm or refute.

It is research, not conclusions: the actual numbers come from running the
sweep on a box that can load the models (see "Running the sweep").

## The knobs and where they live

| Layer | Set by | Flag / field |
|---|---|---|
| Model size + backend | WlK CLI | `--model`, `--backend` |
| Streaming policy | WlK CLI | `--backend-policy` (simulstreaming / localagreement) |
| Decode cadence | WlK CLI | `--min-chunk-size` |
| Rolling-buffer reset | WlK CLI | `--buffer_trimming` (+ `_sec`) |
| Token confirmation | WlK CLI | `--confidence-validation` |
| Native VAD | WlK CLI | `--vac` / `--no-vac` |
| TapScribe speech gate | `LiveConfig` → `SpeechGate` | `gate_speech_threshold`, `gate_hangover_ms`, `gate_pre_roll_ms`, `gate_min_speech_ms` |

`tapscribe.live.build_live_cmd` builds the WlK argv; `tapscribe.speech_gate`
runs the gate. With the default `gate_kind="tapscribe"`, build_live_cmd
passes `--no-vac` (our gate does the gating) and `--confidence-validation`.

## Our current live defaults vs upstream defaults

From `tapscribe/__main__.py` and `tapscribe/live.py:LiveConfig`:

| Setting | TapScribe default | Upstream default | Note |
|---|---|---|---|
| Whisper model | **`tiny.en`** | `base`/`medium` (WlK), version-dependent | **Smallest model — prime accuracy suspect.** |
| `gate_kind` | `tapscribe` (→ `--no-vac`) | WlK VAC on | We replace WlK's VAC with our gate. |
| `confidence_validation` | **`True`** (`--confidence-validation`) | off | WlK warns this is *faster but punctuation less accurate*. We opt into the speed/accuracy trade by default. |
| `min_chunk_size` | unset → WlK default | ~1.0 s | More future context per decode = better accuracy + more lag. |
| `buffer_trimming` | unset → WlK default | `segment` | WlK docs: `segment` performs better in their tests; `sentence` needs a segmenter. |
| `backend-policy` (non-nb) | unset → WlK default `simulstreaming` (AlignAtt) | `simulstreaming` | nb-whisper path forces `localagreement`. Worth A/B-ing for the Whisper path. |
| gate speech threshold | `0.5` | Silero `0.5` | Matches Silero's recommended default. |
| gate hangover (→ `min_silence_duration_ms`) | `400 ms` | Silero `100 ms` | More conservative — avoids chopping mid-sentence pauses. |
| gate pre-roll | `300 ms` ring flushed on open | Silero `speech_pad_ms` `30 ms` | Much larger lead-in recovery than Silero's pad — recovers leading consonants. |
| gate `min_speech_ms` | `0` (open on first VAD "start") | — (no Silero equivalent) | `0` lets one-frame blips open the gate. |

### Smells worth chasing

1. **`tiny.en` is the live default.** The weakest Whisper model. The
   single most likely cause of poor live quality.
2. **`confidence_validation=True` by default.** Upstream explicitly says
   it trades punctuation accuracy for speed. We enable it unconditionally.
3. **The gate over-trims, badly, on noisy/continuous audio — confirmed
   with real data** (see the gate-only benchmark below). On
   `armstrong-en.wav` the default gate forwards only **~21 %** of the
   audio and opens **once** (3.8–6.0 s), dropping the entire first phrase
   *"That's one small step for man"* even though the clip is continuous
   speech end-to-end. This is the strongest concrete lead for poor live
   quality.

## Gate-only benchmark (real data, model-free)

The `SpeechGate` is the front half of the live path and runs entirely
locally (Silero VAD loads with no network), so we can measure exactly how
much audio each gate config forwards **without an ASR model**:

```bash
python tools/bench_live.py --gate-only
```

Results on the two fixtures (`fwd%` = frames forwarded to the backend;
`segs` = silence→speech openings):

| fixture | gate | thr | hang | fwd% | segs | kept_s / audio_s |
|---|---|---|---|---|---|---|
| armstrong-en | tapscribe | 0.50 | 400 | **20.8** | 1 | 2.5 / 12.0 |
| armstrong-en | tapscribe | 0.30 | 400 | 22.5 | 1 | 2.7 / 12.0 |
| armstrong-en | tapscribe | 0.70 | 400 | 20.8 | 1 | 2.5 / 12.0 |
| armstrong-en | tapscribe | 0.50 | 800 | 24.0 | 1 | 2.9 / 12.0 |
| armstrong-en | **backend** | — | — | **100.0** | 1 | 12.0 / 12.0 |
| marlene-nb | tapscribe | 0.50 | 400 | **91.6** | 4 | 13.7 / 15.0 |
| marlene-nb | backend | — | — | 100.0 | 1 | 15.0 / 15.0 |

**Reading it:**

- The Armstrong clip is continuous speech at −12 to −21 dBFS (no real
  silence — verified by an RMS energy profile), yet the gate opens for a
  single 2.2 s window at the **loudest** passage and discards the rest.
  Lowering `gate_speech_threshold` to 0.3 barely changes it (22.5 %), so
  this is **not** a simple threshold tweak — Silero classifies the quieter
  (but still clearly-spoken) passages as non-speech on this noisy old
  recording. In production a model gets only that 2.2 s, so most words are
  never transcribed.
- The Marlene clip (a clean studio reading) forwards ~92 % across 4
  segments — the gate behaves well on clean speech. **So the failure is
  audio-dependent: the gate collapses on noisy / low-level / continuous
  speech, which is exactly what real meeting audio looks like.**
- `gate_kind=backend` (no TapScribe gate; WlK's own VAC) forwards 100 %.
  A strong A/B candidate, and the quickest mitigation to validate.

This points the live-quality investigation squarely at the gate. Open
questions for the code review / full sweep: is the single-open behaviour a
Silero limitation on noisy audio, or a bug in `SpeechGate.feed()` /
`make_silero_vad` (e.g. missing `speech_pad`, the 512-vs-320 sample
buffering, or the gate never re-opening)? The `gate_kind` A/B in the full
sweep quantifies the transcription cost directly (deletions).

## WhisperLiveKit recommendations (upstream)

- **`--buffer_trimming segment`** is the better-tested default and needs
  no sentence segmenter; `sentence` trims on confirmed punctuation but
  needs the segmenter installed.
- **`--min-chunk-size`** should align with the frontend's chunk cadence;
  larger = more context per decode = better accuracy but more lag. Tune
  against the per-tap lag reading.
- **`--backend auto`** picks MLX on macOS, then Faster-Whisper, then
  Whisper. On Apple Silicon, MLX is the fast path (the harness sets
  `--backend mlx-whisper` when `use_mlx`).
- **`--backend-policy`**: `simulstreaming` (AlignAtt, the default) vs
  `localagreement`. AlignAtt has `--frame-threshold` (default 25) for the
  speed/accuracy trade.
- General advice: **benchmark latency + accuracy for your model and VAD
  settings on your actual device**, using `--warmup-file` and watching
  CPU/latency. That is exactly what `tools/bench_live.py` automates.

## Silero VAD recommendations (upstream)

- **`threshold` 0.5** is a good lazy default; tune per dataset. (We use
  0.5.)
- **`min_silence_duration_ms` 100 ms** default — silence to wait before
  closing a chunk. (We map `gate_hangover_ms=400 ms` here — more
  conservative, fewer mid-sentence cuts.)
- **`speech_pad_ms` 30 ms** default — pads each side of a chunk. (We don't
  use Silero's pad; our `gate_pre_roll_ms=300 ms` ring buffer does the
  lead-in recovery, much larger.)
- Sampling rate must be 16 kHz for `VADIterator` (our wire format).

## Hypotheses for the sweep to test

1. Moving `tiny.en` → `base.en` → `small.en` drops WER substantially
   (substitutions fall). **Most likely the main fix.**
2. `gate_kind=backend` vs `tapscribe`: if the tapscribe gate is clipping
   speech, the `tapscribe` rows show more **deletions** than `backend`.
3. `confidence_validation` off improves accuracy (at some latency cost) —
   add a sweep row toggling it once 1–2 are understood.
4. `min_chunk_size=1.0` and/or `buffer_trimming=segment` change the
   latency (`lag_*`, `final_delay_s`) / accuracy balance.

## Running the sweep

```bash
pip install -e ".[whisper,bench]"          # whisperlivekit + faster-whisper + jiwer
python tools/bench_live.py --sweep         # matrix over every fixture → bench-results/*.json
# or a single config, detailed:
python tools/bench_live.py tests/fixtures/audio/armstrong-en.wav --model base.en
```

On Apple Silicon the harness auto-selects the MLX backend; elsewhere it
uses faster-whisper (CPU/CUDA). The metrics to read first: **WER** plus
the **sub / del / ins** split (substitutions ⇒ weak model, deletions ⇒
dropped/clipped speech, insertions ⇒ hallucination), then **lag** and
**gate forward %**.

> **Note on the managed dev box:** model weights download from
> HuggingFace, which the Claude-Code-on-the-web network policy blocks
> ("Host not in allowlist"). The harness reports this as a clean per-run
> error rather than hanging, but the sweep itself must run on a machine
> with model access (e.g. your Mac). The scoring, gate, and framing paths
> were validated here without a model.

## Sources

- [WhisperLiveKit (GitHub)](https://github.com/QuentinFuxa/WhisperLiveKit)
- [WhisperLiveKit default & custom models](https://github.com/quentinfuxa/whisperlivekit/blob/main/docs/default_and_custom_models.md)
- [Silero VAD (GitHub)](https://github.com/snakers4/silero-vad)
- [ufal/whisper_streaming (the streaming algorithm WlK builds on)](https://github.com/ufal/whisper_streaming)
