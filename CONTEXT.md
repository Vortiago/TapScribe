# TapScribe — domain glossary

Canonical names for the concepts that show up across the codebase. When in
doubt, prefer these over synonyms. Add new entries as design conversations
crystallize them; don't introduce shadow vocabulary in code or docs.

## Recorder

The TapScribe Python application itself: the FastAPI server, the per-WAV
`/record` WebSocket handler, the session bookkeeping, the dashboard, and
the orchestrator for the live channel. Operators think of it as their
meeting recorder, hence the name — but its job extends to running and
supervising the Transcribers (both live and batch).

Verb/noun split: **the Recorder** writes **a recording** (a single WAV
per utterance per speaker) via the `/record` WebSocket endpoint. The
process-wide `RECORDING_ENABLED` flag controls whether new recordings
get accepted; it does not stop live transcription, which is independent.

There is one Recorder instance per Python process. The class name
introduced by the candidate-#5 refactor will be `Recorder`.

## Transcriber

The protocol-level abstraction for "something that can transcribe one WAV":
`transcribe(path, *, initial_prompt, hotwords, source_lang, target_lang) -> TranscriptionResult`.

Concrete implementations:
- `FasterWhisperTranscriber` — faster-whisper / CTranslate2 on CPU **or
  CUDA** (compute_type=int8 / float16 respectively). Also serves
  NB-Whisper checkpoints (the `nb-whisper-*` family loads via the same
  backend on its `ct2/` weights).
- `MlxWhisperTranscriber` — mlx-whisper on Apple Silicon GPU.
- `VoxtralTranscriber` — Mistral Voxtral via HuggingFace transformers
  (CUDA / CPU).
- `MlxVoxtralTranscriber` — Voxtral via the `mlx-voxtral` community
  port.
- `MlxParakeetTranscriber` — NVIDIA Parakeet TDT 0.6B via
  `parakeet-mlx` (Apple Silicon).
- `ParakeetTranscriber` — NVIDIA Parakeet TDT 0.6B via the HF
  transformers `AutoModelForTDT` pipeline (CUDA / CPU).
- `MlxCanaryTranscriber` — NVIDIA Canary 1B v2 via `mlx-audio` (Apple
  Silicon). Supports translation + 25 EU langs.
- `CanaryTranscriber` — NVIDIA Canary 1B v2 via NeMo Toolkit
  (CUDA / CPU).

Each Transcriber declares its `device`, `name`, and `backend`. The
`name` is the family label (`"faster-whisper"`, `"mlx-whisper"`,
`"voxtral"`, `"parakeet"`, `"canary"`) — the same string lands in
result JSON under `"transcriber"`. The `backend` field
(`"faster-whisper"`, `"mlx-whisper"`, `"hf-transformers"`,
`"mlx-voxtral"`, `"parakeet-mlx"`, `"parakeet-hf"`,
`"canary-nemo"`) disambiguates which runtime did the work.
`"canary-mlx"` exists as an adapter but is not wired into the catalog
(no published mlx-audio Canary weights).

Note: there is also a **LiveChannel** (a Protocol — see below) — the
`whisperlivekit-server` child process the Recorder supervises for
streaming captions, wrapped by `WhisperLiveKitChannel`. It is not a
`Transcriber` in the protocol sense (no `transcribe(path, …)` call);
when precision matters, say "the live channel" or "the WhisperLiveKit
child" rather than "the live Transcriber."

## TranscriberRegistry

The single declarative source of truth for every model TapScribe knows
about. Lives in `tapscribe/transcribers/catalog.py` as the module-level
`REGISTRY` instance.

Each entry (`ModelEntry`) declares:
- `model_id` — canonical short name (e.g. `parakeet-tdt-0.6b-v3`)
- `family` — one of `whisper`, `nb-whisper`, `voxtral`, `parakeet`,
  `canary`. Drives `<optgroup>` labelling in the dashboard.
- `languages` — ISO codes, or `("auto",)` for auto-detecting models
- `contexts` — frozenset of `"batch"` / `"live"` — gates which
  picker shows the model
- `backends` — tuple of `BackendBinding(kinds, loader)` entries, walked
  in order on `resolve()`
- `inputs` — tuple of `ModelInput`s (see below) the dashboard renders
  for this model

The factory `load_transcriber(model_name, *, backend)` consults the
registry: it picks the first backend binding whose `kinds` contains
the resolved `BackendKind`, then calls its `loader(model_id, kind)`.

Adding a new model = one new `ModelEntry` in `_DEFAULT_ENTRIES`.
Adding a new family of models = one input tuple + one bindings tuple,
both reusable across all variants of the family.

## BackendKind / BackendPreference / available_backends

`BackendKind = Literal["mlx", "cuda", "cpu"]` — the concrete
hardware/runtime kinds a model can resolve to.

`BackendPreference = Literal["auto", "mlx", "cuda", "cpu"]` — what
the *operator* picks (the dashboard's backend-chip row). `auto`
resolves at call time by walking `("mlx", "cuda", "cpu")` and
returning the first kind that's both **available on this machine**
AND **supported by the model**. This keeps NB-Whisper (no MLX
weights) silently routing to CPU on Apple Silicon under `auto`
while still failing loudly when the operator explicitly asks for
MLX on a model that doesn't support it.

`available_backends()` probes the runtime once (importable `mlx`,
`torch.cuda.is_available()`) and caches the result; `/api/state`
and `/api/models` surface it so the dashboard can gray out chips
for backends not installed on the server.

**Disambiguation — picker vocabulary vs runtime vocabulary:**
`tools/install_picker.py` exposes its own `BackendDef` and
`FamilyChoice.backend` strings (`"cpu"` / `"mlx"` / `"both"`). These
describe what pyproject extras pip should install *before* TapScribe
runs — not what the runtime selects at transcribe time. The picker
treats `"cpu"` as "the torch / faster-whisper / NeMo wheels"
(runtime resolves CPU vs CUDA itself), and `"both"` means "install
both atomic extras so the runtime can switch". After install the
runtime side takes over with `BackendKind` / `BackendPreference`
above; the picker has no presence there.

## ModelInput — TextInput / SelectInput

The per-model UI form-field declarations the registry attaches to
each `ModelEntry`. The dashboard reads them from `/api/models` and
renders form fields accordingly; the `/api/transcribe` and
`/api/transcribe-session` routes forward only the values the
registry says the adapter accepts. Adapters that don't consume a
given input ignore the kwarg but echo the value into the result's
audit fields (`initial_prompt_used`, `hotwords_used`,
`source_language`).

Two kinds today:
- `TextInput(name, label, kind="text"|"textarea", placeholder,
  description)` — for `initial_prompt` and `hotwords`.
- `SelectInput(name, label, options, default, description)` —
  for Canary's `source_lang` and `target_lang` dropdowns.

`ModelInput = TextInput | SelectInput` is the union. New input
kinds are added by extending the union, adding a renderer in
`web/js/components/session-detail.js`, and giving them a
discriminator value in `to_mapping()`.

## LiveChannel · WhisperLiveKitChannel

`LiveChannel` is now a runtime-checkable Protocol declared in
`tapscribe/live.py`. The Recorder holds one `LiveChannel` instance
(typed by Protocol); the concrete implementation today is
`WhisperLiveKitChannel`, which encapsulates the supervised
`whisperlivekit-server` child process — same code that used to live
in the unsuffixed `LiveChannel` class.

A follow-up PR will add `ParakeetLiveChannel` (rolling-chunk
pseudo-streaming on `parakeet-mlx` / NeMo) without touching the
Recorder. That's the whole point of the seam.

The dashboard's live-channel picker reads `/api/models?context=live`,
which excludes Parakeet/Canary while only true-streaming families
(Whisper, NB-Whisper, Voxtral) light up.

Each `LiveChannel` declares a class attribute
`supports_native_vad: bool` so the dashboard (and the `/api/live/start`
boundary check) can refuse `gate_kind="backend"` against channels
that have no native VAD to defer to. `WhisperLiveKitChannel` is
True; the planned `ParakeetLiveChannel` will be False.

## SpeechGate · gate_kind

Per-`/tap` Silero-backed speech gate sitting between
`TapFanOut.write_frame` and the live relay. Holds a pre-roll ring
buffer and forwards PCM to WlK only during detected speech bursts —
recovers the leading consonants of each utterance that WlK's own
`--vac` was eating.

Operator-facing knob is `LiveConfig.gate_kind`
(`Literal["tapscribe", "backend"]`, default `"tapscribe"`), which
replaces the old `vac: bool` field — `vac=True` ↔ `gate_kind="backend"`,
`vac=False` ↔ `gate_kind="tapscribe"`. The dashboard's "speech filter"
selector binds to this. Three knobs are tunable from the same panel:
`gate_speech_threshold` (Silero probability), `gate_hangover_ms`
(VAD silence-to-close), `gate_pre_roll_ms` (audio replayed on open).

**Migration note for existing operators**: the default flips behaviour
— with `gate_kind="tapscribe"` (default), WlK runs with `--no-vac` and
TapScribe's gate is the only thing deciding speech vs. silence. To
keep the old behaviour, set `gate_kind="backend"` via the dashboard
or POST `/api/live/start` with `{"gate_kind": "backend"}`. The flip
is intentional — `tapscribe` recovers leading/trailing words that
`--vac` was eating; see PR #51.

## TranscriptionResult

The frozen-dataclass return value of `Transcriber.transcribe(...)`. Carries
the segments, the joined plain text, the metadata about which transcriber
+ model + device produced it, and the inputs that were in effect
(`initial_prompt_used`, `hotwords_used`, `source_language`,
`target_language`, `quality_settings`).

`source_language` records the language the model was told to expect
(or auto-detected); `target_language` is non-empty only when a
translation-capable adapter (Canary today) was asked to translate
(`source_lang != target_lang`). The dashboard renders a translation
badge whenever `target_language` is non-empty.

Post-processors (currently just `hallucinations.apply`, possibly future
PII / phrase-replacement steps) consume a `TranscriptionResult` and
return a new one via `dataclasses.replace` — they never mutate in place.
After `hallucinations.apply` runs, `suppressed_hallucinations` holds the
dropped segments with their `matched_rule` annotated.

## LiveChannel · ActiveStreams · LiveTranscripts

Three internal dataflows the Recorder maintains. The Bridge only ever
opens **one** thing — the `/tap` WebSocket — and the Recorder fans the
audio out to LiveChannel and writes it to disk for ActiveStreams. Settled
caption lines come back from LiveChannel and land directly in
LiveTranscripts. The dashboard's three panels each map to one of these.

| Concept | What it owns | Where it gets its data | Dashboard panel |
|---|---|---|---|
| **LiveChannel** | The supervised `whisperlivekit-server` child process (port 8000). | Bytes relayed by the Recorder from each open `/tap` WS — one internal client connection per `/tap` WS, so settled lines stay attributable to a single speaker. | "live channel" |
| **ActiveStreams** | The map of currently-open `/tap` WebSockets that are writing per-utterance WAVs. | One Bridge WebSocket per remote participant per utterance, raw PCM frames. | "active streams" |
| **LiveTranscripts** | A bounded in-memory deque of settled caption lines (max 200). | Settled lines consumed by the Recorder from its WhisperLiveKit relays, attributed to the originating `/tap` WS's `identity` / `name`. | "live transcripts" |

The Bridge produces audio for exactly one place (`/tap`); the Recorder is
the orchestrator that routes those bytes into all three concerns.

## TapFanOut

The per-`/tap`-WebSocket lifecycle object that owns one Bridge
utterance's audio fan-out. ADR-0002 says "the Recorder owns the fan-out
internally"; TapFanOut is the concrete thing that does it. The `/tap`
route builds one per WS, pumps each PCM frame into `write_frame`, and
relies on its async context manager for cleanup.

Concerns the fan-out owns (the route knows none of these):

- WAV-file open / resume-via-`UtteranceIndex.try_resume` / writeframes /
  finalize / unlink-when-empty.
- UtteranceIndex `register_new` / `release` bookkeeping.
- ActiveStream registration and per-frame `bytes_received` updates.
- WlKRelay create / connect / send / close-with-drain (when `do_live`
  and the LiveChannel is running), including the `on_settled_line`
  callback that cleans Whisper meta-tokens and appends to LiveTranscripts.

Lives in `tapscribe/tap_fan_out.py`. One instance per `/tap` WS;
construction takes `do_record` / `do_live` snapshotted at WS open from
`recorder.tap_settings.get(identity)`, mirroring the global recording
toggle's "next-utterance, not this one" semantics.

## Bridge

The platform-side audio tap that forwards remote-participant PCM to the
Recorder. Typically a browser extension (Chrome MV3 today) but can be
any native helper for a different platform — Teams add-in, Zoom plugin,
etc. Bridges live in `bridges/<platform>-bridge/`.

Wire contract: one WebSocket per utterance to `ws://<recorder-host>/tap?identity=…&name=…`,
streaming raw 16 kHz mono int16 PCM frames (20 ms / 640 bytes per frame).
That's the entire contract — the Recorder fans the audio out internally
to a per-WS WhisperLiveKit relay for live captioning *and* to a WAV on
disk. Bridges don't talk to WhisperLiveKit themselves and don't POST
settled lines back; the verb the Bridge performs is "tap," and the
endpoint name reflects that.

Besides the audio `tap`, a Bridge may issue one **control** verb:
`POST /api/tap/new-session` (authenticated by the tap token as an
`Authorization: Bearer` header) asks the Recorder to rotate to a fresh
session — e.g. the SpatialChat Bridge's "New session" button or its opt-in
"new session on room change." It rotates only; pruning empty sessions stays a
dashboard/Basic-auth action. It's the only thing a Bridge sends over HTTP;
everything else is PCM over `/tap`.

The mnemonic: **TapScribe** = Bridge (the Tap) + Recorder (the Scribe).

## Utterance

A continuous speech segment from one speaker, delimited by mute
boundaries on the Bridge side. One utterance maps to one WAV on disk
and — most of the time — to one `/tap` WebSocket. When the network
blips mid-utterance, the Bridge reconnects with the same `utterance_id`
and the Recorder appends to the existing WAV rather than starting a new
one (see `UtteranceIndex` in `tapscribe/app.py`), so an utterance is a
logical concept that can span multiple physical `/tap` WSes.

Lifecycle: Bridge detects speech → opens `/tap` → streams PCM → Bridge
detects mute → drains any trailing PCM → Bridge closes `/tap` → Recorder
finalizes the WAV.

## utterance_id

The UUID the Bridge generates per utterance and sends as the `?utterance_id=`
query parameter on `/tap`. Stable across reconnects within a single
unmuted segment so the Recorder can resume the same WAV; if the Bridge
omits it, the Recorder mints one. Keyed in `UtteranceIndex` so a returning
WS with a known `utterance_id` (matching identity, file still on disk,
not currently open) appends instead of creating a new file. On-disk
filenames use a *separate* local UUID for collision-safety; don't conflate
the two.

## Mute

The Bridge-side signal that the speaker has stopped talking — on the
SpatialChat Bridge it's the `TrackMuted` event on the input
`MediaStreamTrack`. Mute is the only thing that ends an utterance from
the Bridge's perspective. If audio is still buffered when mute fires
(typical during a network blip), the Bridge enters **drain** mode rather
than closing immediately.

## Drain

The flush of trailing PCM that was buffered on the Bridge when mute fired
but hadn't yet been delivered to the Recorder. On mute, the Bridge keeps
its reconnect ladder running for up to `DRAIN_MAX_MS` (8 s); once a `/tap`
WS reconnects, the buffered audio is flushed to it before the WS closes,
so the WAV finalizes with that trailing audio intact. The timeout exists
so an unreachable Recorder can't wedge the utterance forever. Implemented
in the Bridge's `startDrainTimer()` / `endUtterance()` and on the Recorder
side in `live_relay.close()` / `_flush_tail()`.

## Tail flush

The Recorder-side counterpart to drain, *not* a synonym. After a `/tap`
WS closes, the WhisperLiveKit relay still has one in-flight caption line
that was growing and never got superseded by a newer line. `_flush_tail`
emits that final line so a short utterance producing exactly one caption
doesn't vanish. Lives entirely inside `tapscribe/live_relay.py`; the
Bridge knows nothing about it.

## Invariants

Things the rest of the system relies on. Break one and something
elsewhere will silently misbehave; if you find yourself wanting to relax
one, document why and update this list.

- **One `/tap` WS = one speaker at a time.** Settled live caption lines
  are attributed by the originating `/tap` WS's `identity`/`name`; a WS
  carrying audio from two speakers would scramble attribution in
  LiveTranscripts.
- **One utterance = one WAV.** Reconnects with a known `utterance_id`
  append to the existing file; a fresh `utterance_id` always means a
  fresh WAV. The reverse (one WAV containing multiple utterances) must
  not happen.
- **Bridge → `/tap` is the only audio path.** Bridges don't open
  WhisperLiveKit connections directly and don't POST settled lines back.
  The Recorder owns all fan-out.
- **Drain is bounded.** If the Bridge can't reach the Recorder within
  `DRAIN_MAX_MS`, trailing audio is dropped rather than blocking the
  utterance close forever. Don't remove the timeout.

## Per-WAV transcript cache

Each transcribed WAV gets one or more cached transcripts stored next to
the WAV. The on-disk layout is **directory per WAV** (option a in the
design discussion):

```
<session>/
  alice.wav
  alice.transcripts/
    faster-whisper__small.en.json   ← one sidecar per (backend, model)
    mlx-voxtral__voxtral-mini.json
    _primary                         ← plain-text pointer: "faster-whisper__small.en"
```

- The sidecar filename is `<backend>__<model>.json`, with each component
  sanitized to `[A-Za-z0-9._-]` (other chars → `-`). The authoritative
  `(backend, model)` for a transcript is the JSON inside, not the
  filename — the filename is just a stable index key so we can locate an
  entry in O(1) without scanning.
- `_primary` is a one-line text file holding the `<backend>__<model>`
  key of the entry the merge layer should use. Absent or pointing at a
  missing key → fall back to the newest-mtime sidecar.
- The sidecar JSON wire format inside each file is unchanged from the
  pre-multi-cache era — only the path/naming changes.

**Legacy compatibility.** Older WAVs have a single `<wav>.json` sidecar
sitting alongside the WAV instead of a `<wav>.transcripts/` directory.
The cache reads these transparently. On the first write of a new
sidecar for the same WAV, the legacy file is migrated into the new
layout (renamed to `<backend>__<model>.json` inside
`<wav>.transcripts/`) so the two formats never coexist.

**Cache key.** Each entry's identity is `(backend, model)`. Calling
`cached_transcribe` with a different `(backend, model)` adds a sidecar;
it does not replace the prior one. The legacy fingerprint check
(`wav_size` + `wav_mtime_ns`) is applied per entry — a WAV rewrite
naturally invalidates every per-WAV transcript on its next
`cached_transcribe` call.

**Public API.**
- `read_cached(wav)` returns the primary `CachedTranscription` (or None).
- `read_all_cached(wav)` returns every cached transcript for the WAV.
- `set_primary_transcript(wav, *, backend, model)` flips the pointer.
- `cached_transcribe(wav, transcriber, ...)` is unchanged in signature;
  it just no longer evicts other entries.

## Batch transcription

The orchestrator that drives a `Transcriber` across either one WAV or
a session-range of WAVs, applying the per-session prompt/hotwords
overrides, hallucination filtering, and the cache layer. Lives in
`tapscribe/batch_transcribe.py` and is the layer the `/api/transcribe`
and `/api/transcribe-session` route handlers delegate to.

Two entry points:

- `transcribe_one(recorder, BatchOneRequest) -> dict` — one WAV. Runs
  the silent-WAV pre-check (RMS-floor against the *original*) so the
  operator gets fast feedback on noise files, forces a fresh transcribe
  through the cache, then returns the freshly-written sidecar's raw
  JSON dict.
- `transcribe_session(recorder, BatchSessionRequest) -> dict` — every
  WAV in the supplied `from_iso`/`to_iso` range. Claims a `JobTracker`
  slot (one transcribe/strip in flight per session), loops with progress
  updates, then merges via `merge_session` and writes both
  `session-transcript.json` and `.txt`. No per-WAV silence pre-check —
  the session loop transcribes everything in range.

Both forms share a `TranscriberInvocation` envelope (initial_prompt,
hotwords, source_lang, target_lang, hallucination_rules) resolved once
per request. The prompt/hotwords resolution layers session-meta over
the global config files (`config/prompt.txt`, `config/hotwords.txt`);
an empty session-meta override falls back to the global default.

The module never raises `HTTPException`. It raises domain errors
(`WavTooQuiet`, `WavUnreadable`, `SessionBusy`, `NoUsableWavs`, base
`BatchTranscribeError`) and the route handlers map those to HTTP codes.
This keeps the module FastAPI-free so the same orchestrator can drive
a CLI batch, a queue worker, or future per-region re-transcribes
without re-implementing the chain. The request value objects
(`BatchOneRequest`, `BatchSessionRequest`) are the test surface.

## Wire-format note

Result JSON files (per-WAV `<wav>.transcripts/<...>.json` or legacy
`<wav>.json`, plus `session-transcript.json`) use
`"transcriber": "faster-whisper" | "mlx-whisper" | "voxtral"`. This is a
rename from a prior `"backend"` field; older recordings written before
the rename may still use the old key.
