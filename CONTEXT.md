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
- `ParakeetTranscriber` — NVIDIA Parakeet TDT 0.6B via HF
  `transformers` (`AutoModelForTDT.generate` + `processor.decode`,
  CUDA / CPU). Token timestamps are folded into word/segment alignment
  by `_parakeet_tdt.build_segments_from_tdt_tokens`.

Each Transcriber declares its `device`, `name`, and `backend`. The
`name` is the family label (`"faster-whisper"`, `"mlx-whisper"`,
`"voxtral"`, `"parakeet"`) — the same string lands in result JSON
under `"transcriber"`. The `backend` field (`"faster-whisper"`,
`"mlx-whisper"`, `"hf-transformers"`, `"mlx-voxtral"`,
`"parakeet-mlx"`, `"parakeet-hf"`) disambiguates which runtime did
the work.

> Canary (NVIDIA Canary 1B v2, the former translation backend) was
> removed along with the NeMo dependency — see ADR-0006. NeMo's only
> consumers were Parakeet (now on `transformers`) and Canary; dropping
> both retired `nemo_toolkit` + `kaldialign` entirely. TapScribe no
> longer offers speech translation.

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
- `family` — one of `whisper`, `nb-whisper`, `voxtral`, `parakeet`.
  Drives `<optgroup>` labelling in the dashboard.
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
treats `"cpu"` as "the torch / faster-whisper / transformers wheels"
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

Two kinds:
- `TextInput(name, label, kind="text"|"textarea", placeholder,
  description)` — for `initial_prompt` and `hotwords`.
- `SelectInput(name, label, options, default, description)` — a
  dropdown. No shipped model declares one today (Canary's
  `source_lang`/`target_lang` selects were removed with the family);
  retained for future use, e.g. an explicit Whisper language pin.

`ModelInput = TextInput | SelectInput` is the union. New input
kinds are added by extending the union, adding a renderer in
`web/js/next/components/engine.js`, and giving them a
discriminator value in `to_mapping()`.

## LiveChannel · WhisperLiveKitChannel

`LiveChannel` is now a runtime-checkable Protocol declared in
`tapscribe/live.py`. The Recorder holds one `LiveChannel` instance
(typed by Protocol); the concrete implementation today is
`WhisperLiveKitChannel`, which encapsulates the supervised
`whisperlivekit-server` child process — same code that used to live
in the unsuffixed `LiveChannel` class.

A follow-up PR will add `ParakeetLiveChannel` (rolling-chunk
pseudo-streaming on `parakeet-mlx` / `transformers`) without touching
the Recorder. That's the whole point of the seam.

The dashboard's live-channel picker reads `/api/models?context=live`,
which excludes Parakeet and Voxtral (both batch-only — `build_live_cmd`
has no backend for either) while only the true-streaming Whisper
families (Whisper, NB-Whisper) light up.

Each `LiveChannel` declares a class attribute
`supports_native_vad: bool` so the dashboard (and the `/api/live/start`
boundary check) can refuse `gate_kind="backend"` against channels
that have no native VAD to defer to. `WhisperLiveKitChannel` is
True; the planned `ParakeetLiveChannel` will be False.

`info["device"]` is an **observation, not an assertion**: WlK exposes
no `--device` flag (its faster-whisper backend hands `device="auto"`
to CTranslate2 inside the child), so the parent can't pin or know the
device. The label is seeded with a prediction from the same
`available_backends()` probe the batch chips use (`"CUDA (auto)"` /
`"CPU"`), then overwritten by the log pump when the child's
`Accelerator: …` startup banner reports what it actually sees — the
same observe-the-child pattern that promotes `state` to "running".
Future `LiveChannel` adapters must keep this semantic: report the
device you observed, not the one you hope for.

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
translation-capable adapter was asked to translate
(`source_lang != target_lang`). No shipped adapter translates today
(Canary was removed — see ADR-0006), but the field + the dashboard's
translation badge are retained for back-compat with any sidecar cached
from one that did.

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
| **LiveTranscripts** | A bounded in-memory deque of settled caption lines (max 200). | Settled lines consumed by the Recorder from its WhisperLiveKit relays, attributed to the originating `/tap` WS's `identity` / `name` **and snapshotted `session`**. | "Live captions" |

The Bridge produces audio for exactly one place (`/tap`); the Recorder is
the orchestrator that routes those bytes into all three concerns.

**Live captions are session-scoped and ephemeral.** Each settled line carries
the session it was snapshotted to at `/tap` open — the same attribution the
*Detached session* entry relies on — and the dashboard's **Live captions**
panel shows only the **focused** session's lines, so an archived session never
displays the live session's captions (the isolation the model guarantees on
disk, now honored in the UI). The deque is a bounded in-memory tail (max 200);
a session's durable text is its merged **Transcript**, not the live feed.

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
- ActiveStream registration and per-frame `bytes_received` updates,
  plus the post-gate level meter and the gate-open transition push.
- The **live leg** — but only by holding one **TapRelay** (below) and
  feeding each frame through it. The relay/gate/reconnect machinery
  itself is no longer TapFanOut's concern.

Lives in `tapscribe/tap_fan_out.py`. One instance per `/tap` WS;
construction takes `do_record` / `do_live` snapshotted at WS open from
`recorder.tap_settings.get(identity)`, mirroring the global recording
toggle's "next-utterance, not this one" semantics. `write_frame` writes
the WAV, calls `tap_relay.feed(buf)`, then uses the returned post-gate
frames for the level meter and `gate_open` for the ActiveStream
transition — so the gate's *output* crosses the seam but the gate lives
behind it. Exposes its `TapRelay` as a `relay` property (the read-surface
that replaced the old private-field test backdoors).

## TapRelay

The per-`/tap` **live leg**: the sub-unit of TapFanOut that owns the WlK
relay lifecycle, the per-tap SpeechGate, and the transparent
reconnect-with-backoff across WhisperLiveKit restarts (model swap, child
crash). Extracted from TapFanOut so the reconnect state machine — the
live path's most intricate, most-broken part — has its own interface and
is the test surface, instead of being assertable only by poking
TapFanOut's private fields. Lives in `tapscribe/tap_relay.py`.

Interface:

- `open()` — attach a relay + gate against the LiveChannel's current
  config, but only when `do_live` and the channel is running; otherwise
  stay dormant (a record-only or live-down tap holds an inert TapRelay,
  so TapFanOut needs no `do_live` branch of its own).
- `feed(buf) -> FedFrames` — feed the gate, forward the surviving frames
  to the relay (or schedule a reconnect if the relay died and the
  channel is back up), and hand back `FedFrames(frames, gate_open)` so
  TapFanOut can drive the meter and the ActiveStream gate-open row.
  Recording to disk is unaffected — per ADR-0002 graceful degradation,
  the relay dying never stops the WAV.
- `close()` — cancel any in-flight reconnect, then close the relay
  (draining tail captions; see **Tail flush**).
- read-surface: `reconnect_attempts` (backoff coalesces a burst of
  frames into one attempt) and `connected -> (host, port, language) |
  None` (what the live relay is currently bound to, or `None` when
  dead). These are what tests assert on.

`RelayHandlers(on_settled_line, on_metrics, on_buffer)` is the named
handler contract TapFanOut supplies as bound methods that read the tap's
`identity`/`name`/`session`/`conn_id` at invocation; `WlKRelay` and the gate are
built through injectable `relay_factory` / `gate_factory` seams so the
reconnect/backoff behaviour is unit-testable with a fake relay, no live
WhisperLiveKit child required. `RELAY_RECONNECT_BACKOFF_S` lives here.

This is an internal sub-unit, **not** a new architectural boundary —
ADR-0002 (Bridge → one `/tap` WS → the Recorder fans out internally) is
unchanged. The gate-less future `ParakeetLiveChannel` does not need this
seam; that variation rides the existing `LiveChannel` Protocol +
`supports_native_vad` flag.

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

Besides the audio `tap`, a Bridge may issue a small **control** plane over
HTTP, all authenticated by the tap token as an `Authorization: Bearer`
header (never the cleartext-vulnerable subprotocol slot the `/tap` WS must
use). `POST /api/tap/new-session` asks the Recorder to rotate to a fresh
session (pruning empty sessions stays a dashboard/Basic-auth action); with
body `{"detached": true}` the same verb instead mints a **detached
session** (below) without rotating anything. A Bridge that brackets
meetings also calls the **end-of-meeting pipeline** endpoints
(`POST`/`GET /api/tap/sessions/{session}/pipeline`) to trigger and poll
processing for its detached session — see [Bracketed meeting](#bracketed-meeting).
These HTTP control calls are the only thing a Bridge sends besides PCM over
`/tap`; the surface is create / trigger / poll / rotate / probe, never
delete or prune, so a leaked tap token's blast radius stays bounded.

The mnemonic: **TapScribe** = Bridge (the Tap) + Recorder (the Scribe).

## Bracketed meeting

A **Start meeting → End meeting** bracket a Bridge wraps around a recording
so the user gets meeting notes without ever opening the dashboard (the
SpatialChat Bridge and the Windows tray Bridge both implement it). The
bracket governs Session **routing, not capture**: capture stays automatic —
speakers are tapped as they speak and a [Mute](#mute) still ends the
[Utterance](#utterance), exactly as without a meeting — the bracket only
decides *which* Session new taps feed.

- **Start meeting** mints a fresh [detached session](#detached-session) and
  marks the meeting active. While active, every `/tap` open and reconnect
  carries `&session=<id>`, so the whole meeting lands in one Session even
  across a SpatialChat room change (which performs no Session action of its
  own). When no meeting is active, taps fall back to the Recorder's global
  current session.
- **End meeting** closes every open tap honouring [Drain](#drain), waits for
  a close-all barrier (so the last Utterance's WAV is finalised first), then
  triggers the [end-of-meeting pipeline](#end-of-meeting-pipeline) for the
  detached Session and marks the meeting no longer active (routing falls back
  to global). The trigger carries no model/summarizer/prompt — operator
  defaults only.

The detached Session id is the bracket's **durable handle**: it is persisted
(the in-memory "active" flag is the live routing source of truth; the stored
id is what an ephemeral popup re-reads) and survives End, so a **meeting
card** can poll the pipeline for progress and the finished summary — even
after the popup closed or the Recorder restarted (the *done* branch is served
from the persisted `session-summary.json`). The card holds no local summary
cache; it re-derives from the stored id on each open. The id is cleared only
on the next Start meeting or an explicit Dismiss.

## Detached session

A session a Bridge creates for itself and directs its own taps into,
fully isolated from the Recorder's global current session — so two
people can tap two meetings against one Recorder without muddling.
Minted via `POST /api/tap/new-session` with body `{"detached": true}`
(the global current session is untouched; the directory is created
eagerly, de-collided from the 1 s-resolution current-session id) and
joined by opening `/tap` with `?session=<id>`. The id crosses
`resolve_session_dir` — the canonical path-safety seam — and an unknown
or invalid id refuses the WS upgrade, mirroring token rejection.

Session affiliation is snapshotted at WS open (like the per-identity
`do_record`/`do_live` prefs): rotations never re-home an open tap, and
both the tap's WAVs and its live-feed lines carry the snapshotted
session. On disk and on the dashboard a detached session is an ordinary
session — same layout, listing, and maintenance operations. That
includes empty-session pruning: a detached dir with no WAV yet is
prunable by the dashboard's prune actions, so Bridges should create it
just-in-time, not long in advance — and a tap whose per-identity record
preference is off never materialises a WAV at all, leaving its detached
session prunable for its whole lifetime (a pruned id refuses the next
`?session=` upgrade until the Bridge mints a fresh one).

## Capture device · render device · loopback

The two kinds of audio endpoint a native Bridge can tap (see
`bridges/windows-tray-bridge/`). A **capture device** is an input — a
microphone. A **render device** is an output — speakers/headphones — and
is the **loopback** candidate: capturing its output mix (WASAPI
**loopback** on Windows) records the system audio out, i.e. the "other
side" of a meeting, which has no mute event of its own. Both sit behind
the bridge core's single capture seam (`IAudioCapture`); loopback is just
another capture source, so it flows through the same resample → level gate
→ `/tap` pipeline as a mic. Device **enumeration** (`IAudioDeviceEnumerator`)
lists both kinds tagged by `DeviceFlow` (Capture/Render); the Windows impl
is over NAudio's `MMDeviceEnumerator`. The cross-platform bridge core never
sees a platform audio API — WASAPI is one implementation behind the seam.

## One device = one speaker · CaptureOrchestrator

A Bridge that taps several devices at once runs **one independent pipeline
per device**, each opening its own `/tap` WS under its own stable
`identity`/`name`, so recordings stay attributable per source instead of
mixed into one stream — the coarse "me vs. them" split ahead of real
diarization (#78). Defaults: the microphone streams under the operator's
identity, the system **loopback** under `system`. The bridge core's
**CaptureOrchestrator** owns the set of pipelines: it starts one per
selected device (best-effort — a device that fails to open is surfaced and
skipped while the rest still run), rejects duplicate identities up front
(the Recorder buckets WAVs by the sanitised identity, so a collision would
cross-attribute two devices into one speaker), and tears them all down
concurrently and bounded. The devices co-locate in one **detached
session** (above), so both sides of a meeting land in one folder as
distinct speakers.

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

On platforms with no native mute event (Windows WASAPI loopback emits
none), the **Level gate** (below) synthesises Mute: it opens an utterance
when the input level crosses its threshold and fires Mute after a silence
hangover.

## Level gate

The Bridge-side RMS **level** gate that decides utterance boundaries on
platforms that have no native mute event — it is how the Windows tray
Bridge produces **Mute**. Lives in the cross-platform bridge core
(`GateOptions` / `LevelGate` under `bridges/windows-tray-bridge/`); its
knobs are the **open threshold** (a linear RMS amplitude), the
**hangover** (silence-to-close), and the **pre-roll** (leading audio
replayed when the gate opens so the first consonants aren't clipped).

Distinct from the Recorder-side **SpeechGate**: that one is Silero-backed
and its threshold is a speech *probability*; the Level gate is amplitude
RMS. The two gates live on opposite sides of the `/tap` wire and **never
share threshold units** — UI that surfaces the Level gate must not borrow
SpeechGate's "speech threshold" vocabulary.

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

## Interaction hold

The dashboard-wide rule that a per-tick render defers to operator
interaction state instead of destroying it: a region is **held** (not
re-rendered) while a control inside it is focused, a text selection
starts or ends inside it, or — for tail-following panels — the operator
has scrolled away from the tail. Deferral always skips **without
advancing the render gate**, so the held render lands on the first tick
after the interaction clears; updates are delayed by the operator's own
interaction, never lost.

Mechanics live in `web/js/templates.js`: `renderRegion(host, build, {sig})`
is the swap-based primitive (gates on a focus/selection guard + an optional
perf signature, replaceChildren on render), `selectionInside(host)` is the
shared selection predicate, and `markRegionStale(host)` invalidates a host's
remembered signature so the NEXT `renderRegion` re-renders after a mutate /
lazy-body load — the "defer, don't force" reset (`force:true` would bypass the
guards and clobber a selection). Swap-based copy-target panes render through
`renderRegion` (the Summary output pane, the Transcript merged pane, plus the
spine/settings/live-channel/config-card regions); a few **view-level** gates
that guard a whole `update()` body rather than one host swap (the Recordings
WAV list) apply `selectionInside` directly. The decision and its rejected
alternatives (DOM-diffing, capture-and-restore, pausing the poll) are
ADR-0004. Say "this region needs the interaction hold," not ad-hoc
descriptions of focus/selection guards.

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

## Session modules — paths · listing · maintenance

Recording-session bookkeeping is split across three modules by concern, so the
once-per-second read path isn't tangled with path-safety or destructive
operations:

- **`session_paths`** — the path-resolution seam. The ONE place a request-
  supplied `session` / `name` becomes a filesystem path under `RECORDINGS_DIR`:
  `_safe_part` (the lowest-level sanitiser) + `resolve_session_dir` /
  `resolve_source_dir` / `resolve_wav` / `session_meta_path` / `stripped_dir`.
  Owns the two-layer `py/path-injection` guard (`_safe_part` + the canonical
  `realpath(x).startswith(root + os.sep)` check) so callers cross it instead of
  re-deriving it. New code that turns request input into a recordings path goes
  through here — never `config.RECORDINGS_DIR / <raw>` by hand. It is ALSO the
  one owner of the on-disk session-layout filenames — `FILENAME_TRANSCRIPT_JSON`
  / `_TXT`, `FILENAME_SUMMARY_JSON`, `FILENAME_META_JSON`,
  `FILENAME_STRIP_META_JSON`, and `DIRNAME_STRIPPED` — which readers, writers,
  and maintenance ops compose onto an already-resolved session (or stripped)
  dir instead of hand-typing the literal, so a rename touches one line
  (`test_session_layout.py` pins this). Note `DIRNAME_STRIPPED` (the directory
  name) is deliberately distinct from the `source == "stripped"` API selector
  value in `resolve_source_dir` — a wire enum, free to diverge from the dir
  name.
- **`sessions`** — the dashboard read model: `gather_sessions` (the poll-path
  listing, memoised on cheap stat signatures), `read_session_meta` /
  `write_session_meta`, and the lazy full-transcript reads the slim poll
  markers point at. Read-path only.
- **`session_maintenance`** — destructive, infrequent operator operations:
  `absorb_session`, `delete_session_audio` / `delete_session_wav`,
  `prune_empty_sessions`, `session_is_empty`. Resolves via `session_paths`,
  reads/writes meta via `sessions`.

(The recorder-filename parsers `parse_wav_start` / `parse_wav_speaker_slug` /
`parse_wav_speaker_ident` and `build_recorder_wav_name` all live in `text` — the
single source of truth for the `<iso>_<speaker>_<ident>_<utt>.wav` format.)

## Session job · `JobTracker.run`

The "one heavy job per session at a time" rule — a session may have at most
one transcribe **or** strip **or** summarize **or** end-of-meeting pipeline
running. `JobTracker` (a Recorder
sub-component in `tapscribe/recorder.py`) holds the per-session `JobState`; the
batch orchestrators bracket their work with the async context manager
`recorder.jobs.run(session, *, kind, total, …)`:

```python
async with recorder.jobs.run(session, kind="summarize", total=1) as job:
    await job.update(current=i, current_file=name)   # progress, optional
    ...
```

`run` claims the slot on entry and **releases it on every exit path**. When the
session is already busy it raises `SessionBusy` *before* the block runs, so a
foreign claim is never released — the guard is structural, not a try/finally
discipline each orchestrator re-derives (which is what the three of them used
to do by hand, and one diverged). `SessionBusy` lives next to `JobTracker` in
`recorder.py` because it's a *job* concept, not a transcription one; new batch
orchestrators raise it only via `run`, never by importing it sideways.

The read side (`jobs.snapshot()` / `jobs.get()`) is unchanged and widely used:
`/api/state` surfaces each session's `progress`, and the delete/absorb routes
refuse a session with a job in flight.

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
  WAV in the supplied `from_iso`/`to_iso` range. Brackets the loop in
  `recorder.jobs.run` (the Session job seam), reporting progress through the
  yielded handle, then merges via `merge_session` and writes both
  `session-transcript.json` and `.txt`. No per-WAV silence pre-check —
  the session loop transcribes everything in range.

Both forms share a `TranscriberInvocation` envelope (initial_prompt,
hotwords, source_lang, target_lang, hallucination_rules) resolved once
per request. The prompt/hotwords resolution layers session-meta over
the global config files (`config/prompt.txt`, `config/hotwords.txt`);
an empty session-meta override falls back to the global default.

The module never raises `HTTPException`. It raises domain errors — its own
`WavTooQuiet` / `WavUnreadable` (under `BatchTranscribeError`), plus
`SessionBusy` (from `recorder`, via `jobs.run`) and `NoUsableWavs` /
`InvalidRange` (selection verdicts, from `session_merge`). A single
domain-error handler registered in `app.py` maps each error type to its HTTP
code once (busy → 409, …), so routes are just `return await orchestrator(req)`
rather than per-route try/except ladders. Keeping the orchestrator FastAPI-free
means the same code can drive a CLI batch, a queue worker, or future per-region
re-transcribes without re-implementing the chain. The request value objects
(`BatchOneRequest`, `BatchSessionRequest`) are the test surface.

## Batch strip

The strip-silence sibling of Batch transcription — same orchestrator
shape, same FastAPI-free contract. Lives in `tapscribe/batch_strip.py`;
the `/api/sessions/{session}/strip-silence` route handler is a thin
parse-and-map shim over it.

One entry point: `strip_session(recorder, StripSessionRequest) -> dict`
— brackets its work in `recorder.jobs.run` (the Session job seam, shared with
Batch transcription), loops `strip_one_wav` (the per-WAV splitter, which lives
in `batch_strip` as the unit of work this orchestrator owns) over every original
WAV on a worker thread, and aggregates. Raises `SessionBusy` (from `recorder`, via `run`) /
`NoUsableWavs` (from `session_merge`) plus its own `StrippedDirUnclearable`
(under a `BatchStripError` base — a strip failure isn't a transcription one)
when a previous `stripped/` can't be cleared. `StripSessionRequest` owns the
knob defaults (`min_silence_ms`, `pad_ms`, `speech_floor_db`); the route
forwards only explicitly-provided values, and is the test surface.

## Batch summarize

The post-transcription sibling of Batch transcription / strip — same
orchestrator shape (bracket the Session job slot via `recorder.jobs.run`, run
off the event loop), same FastAPI-free contract. Lives in
`tapscribe/batch_summarize.py`; the `/api/sessions/{session}/summarize` route is
a thin shim. One entry point: `summarize_session(recorder,
SummarizeSessionRequest) -> dict` — reads the session's merged transcript
(`NoMergedTranscript` when absent/empty), builds a `Summarizer` via the factory
(`SummarizerUnavailable` for a misconfigured source — *before* the slot claim,
so a bad command fails fast), runs it under the `summarize` job kind, and
returns the summary dict. As of the tracer-bullet slice (#82) there's no
persistence (the summary is lost on reload) and no saved config (source /
command / prompt arrive per request).

## End-of-meeting pipeline

The strip → batch-transcribe(stripped) → summarize chain as **one** Session
job: one trigger takes a finished session from raw WAVs to a persisted
summary with no operator in the loop. The fourth orchestrator sibling, in
`tapscribe/batch_pipeline.py` — it owns no work loop of its own; each of the
three siblings exposes a `*_locked` core (its work minus the slot claim), and
the pipeline drives those under a single `kind="pipeline"` claim so the
one-heavy-job rule holds across the whole chain (a concurrent trigger or a
manual transcribe gets 409 for the chain's full duration). Stage progress
flows through the ordinary job snapshot — `JobState.stage` names the current
stage and the per-stage loops keep updating `current`/`total` — so the
dashboard's job bar shows "Pipeline · Transcribing 3/12" with no extra
plumbing.

Triggered and polled by a Bridge over two tap-bearer endpoints
(`POST`/`GET /api/tap/sessions/{session}/pipeline`). **Operator defaults
only**: the trigger's body is ignored, never parsed — `PipelineRequest`
carries just the session id, and the pipeline resolves the batch model from
`batch-model.txt` (catalog-validated at write AND at read), the backend from
the Recorder's launch preference, and the summarizer from the Local source's
bundled/env default — so a tap-token holder can never choose what gets loaded
or downloaded (the Summarizer catalog/allowlist invariant, one privilege
boundary up).

`start_pipeline` claims the slot **in the request path** (deterministic 409)
and runs the chain in a background task — the one sanctioned hand-rolled
claim/release, since claim and release live in different call frames; the
release only ever runs in the task spawned after a successful claim. A stage
failure aborts the chain and is recorded in `recorder.pipelines`
(`PipelineResults`, in-memory, one record per session, overwritten on
re-trigger) as failed-at-stage with the domain error — the poll endpoint's
contract. "Done" is answered from the persisted `session-summary.json`, so a
Bridge polling across a Recorder restart still gets its summary.

## Summarizer

The protocol-level abstraction for "something that can summarize one
transcript": `summarize(transcript, *, prompt) -> SummaryResult`. The
post-transcription mirror of `Transcriber`, one altitude up. Lives in the
`tapscribe/summarizers/` **package** (it graduated from a single module once the
Local source landed, mirroring `transcribers/`); the factory
`load_summarizer(source=…)` in `__init__` dispatches on source — exactly as
`load_transcriber` resolves a backend.

Package layout:
- `base` — the `Summarizer` protocol, `SummaryResult`, the domain errors, and
  `DEFAULT_SUMMARY_PROMPT`. The leaf everything builds on.
- `command` — `CommandSummarizer`: pipes the merged transcript to an
  operator-supplied CLI tool (e.g. `claude -p`) on **stdin**, reads the summary
  from **stdout**. Argv is list-form (`shlex.split` + the prompt as a trailing
  positional), `shell=False`, timeout-bounded — same subprocess discipline as
  `build_live_cmd`.
- `local` — `LocalSummarizer`: a bundled, offline, hardware-routed model
  (MLX on Apple Silicon, GGUF/CPU elsewhere), lazy-imported on first
  `summarize()`.
- `catalog` — the source-neutral model catalog (`SUMMARY_MODELS`), the
  allowlist (`_is_allowed_local_model`), per-machine hardware routing, and the
  operator knobs. Serialised by `GET /api/summarize/models`; the catalog is
  ALSO the security allowlist (an untrusted request-body model id only reaches
  `mlx_lm.load` / a Hub download if it's listed). `ApiSummarizer`'s model list
  will land here too.

**Command preset** — a curated `COMMAND_PRESETS` row in `catalog`
(key / label / template / note) that a dashboard dropdown pick seeds into the
Command source's **editable** template field. Unlike the local-model catalog,
the preset list is NOT an allowlist — the command template stays
operator-trusted free text; what a preset adds is hardening-by-default
(the Claude Code row ships with tool use disabled so a prompt-injected
transcript can't make the tool read files or fetch URLs). Don't confuse the
two catalogs' roles when adding rows to either.

`ApiSummarizer` (OpenAI-compatible / Ollama, #85) is the remaining planned
adapter behind the same one-method seam — one new `api` module. Adapter-level
errors are `SummarizerUnavailable` (misconfigured / not-wired source → 400) and
`SummarizerFailed` (ran but failed → 502).

## Wire-format note

Result JSON files (per-WAV `<wav>.transcripts/<...>.json` or legacy
`<wav>.json`, plus `session-transcript.json`) use
`"transcriber": "faster-whisper" | "mlx-whisper" | "voxtral"`. This is a
rename from a prior `"backend"` field; older recordings written before
the rename may still use the old key.
