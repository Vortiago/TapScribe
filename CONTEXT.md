# TapScribe — domain glossary

Canonical names for the concepts that show up across the codebase. When in
doubt, prefer these over synonyms. Add new entries as design conversations
crystallize them; don't introduce shadow vocabulary in code or docs.

## Recorder

The TapScribe Python application itself: the FastAPI server, the per-WAV
`/tap` WebSocket handler, the session bookkeeping, the dashboard, and
the orchestrator for the live channel. Operators think of it as their
meeting recorder, hence the name — but its job extends to running and
supervising the Transcribers (both live and batch).

Verb/noun split: **the Recorder** writes **a recording** (a single WAV
per utterance per speaker) via the `/tap` WebSocket endpoint. The
per-instance `recorder.recording_enabled` flag controls whether new
recordings get accepted; it does not stop live transcription, which is
independent.

There is one `Recorder` instance per Python process (`tapscribe/recorder.py`).

## Transcriber

The protocol-level abstraction for "something that can transcribe one WAV":
`transcribe(path, *, initial_prompt, hotwords, source_lang) -> TranscriptionResult`.

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
`"mlx-whisper"`, `"voxtral-hf"`, `"mlx-voxtral"`,
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
- `family` — one of `whisper`, `nb-whisper`, `voxtral`, `parakeet`,
  `moonshine`. Drives `<optgroup>` labelling in the dashboard. Moonshine
  (PRD #120, shipped in #334) is registered live-only
  (`contexts={"live"}`): its two entries
  (`moonshine-tiny`/`moonshine-base`) carry real loaders gated on
  per-runtime probe modules (`mlx_audio` on Apple Silicon,
  `moonshine_onnx` elsewhere), so `/api/models?context=live` lists them
  exactly when the matching runtime is installed. There is deliberately
  no batch adapter (PRD #120 Out of Scope) — resolving a Moonshine id
  for batch raises `NotImplementedError`.
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
`tapscribe/install_picker.py` exposes its own `BackendDef` and
`FamilyChoice.backend` strings (`"cpu"` / `"mlx"` / `"both"`). These
describe what pyproject extras pip should install *before* TapScribe
runs — not what the runtime selects at transcribe time. The picker
treats `"cpu"` as "the torch / faster-whisper / transformers wheels"
(runtime resolves CPU vs CUDA itself), and `"both"` means "install
both atomic extras so the runtime can switch". After install the
runtime side takes over with `BackendKind` / `BackendPreference`
above; the picker has no presence there.

## ModelInput — TextInput

The per-model UI form-field declarations the registry attaches to
each `ModelEntry`. The dashboard reads them from `/api/models` and
renders form fields accordingly; the `/api/transcribe` and
`/api/transcribe-session` routes forward only the values the
registry says the adapter accepts. Adapters that don't consume a
given input ignore the kwarg but echo the value into the result's
audit fields (`initial_prompt_used`, `hotwords_used`,
`source_language`).

One kind today: `TextInput(name, label, kind="text"|"textarea",
placeholder, description)` — for `initial_prompt` and `hotwords`.
(A `SelectInput` dropdown kind existed for Canary's language selects
and was deleted with no remaining users — one adapter is a
hypothetical seam; zero is dead code.)

`ModelInput = TextInput` is the (degenerate) union. A new input
kind is added by widening the union, adding a renderer in
`web/js/next/components/engine.js`, and giving it a
discriminator value in `to_mapping()` — all in the same PR as the
model that actually declares it.

## LiveChannel · WhisperLiveKitChannel · MoonshineLiveChannel

`LiveChannel` is now a runtime-checkable Protocol declared in
`tapscribe/live.py`. The Recorder holds one `LiveChannel` instance
(typed by Protocol); the concrete implementations today are
`WhisperLiveKitChannel` (encapsulates the supervised
`whisperlivekit-server` child process — same code that used to live
in the unsuffixed `LiveChannel` class) and `MoonshineLiveChannel`
(see below, PRD #120) — the Recorder never touches the concrete class
directly.

Both concrete channels inherit **`LiveChannelBase`** (`live.py`): the
shared transition surface — `matches` / `apply_gate_knobs` /
`begin_transition` and the `info` gate-mirror — lives there ONCE, so the
two engines no longer copy it byte-for-byte (and Moonshine no longer
reaches across the module boundary for `live.py` privates). The only
per-engine divergences are two capability flags: `fixed_language`
(Moonshine `"en"`; `None` = multilingual — a language change never forces
a restart and `info` reports that fixed language) and
`supports_confidence_validation` (Moonshine `False` —
`info` reports that field as `""` rather than a misleading on/off, keeping
its `/api/state` payload unchanged, AND `matches` ignores a `conf` change
so it never forces a restart the engine wouldn't honour). Engine
construction and `running` /
`start` / `stop` stay in the subclass; `supports_native_vad` (already
per-engine) rides alongside. The `LiveChannel` Protocol remains the seam
the Recorder types against (ADR-0003); the base is one implementation of
it, and a from-scratch implementer is still free to satisfy the Protocol
directly.

A follow-up PR will add `ParakeetLiveChannel` (rolling-chunk
pseudo-streaming on `parakeet-mlx` / `transformers`) without touching
the Recorder. That's the whole point of the seam — it inherits the base
for the shared surface, or implements the Protocol directly.

The dashboard's live-channel picker reads `/api/models?context=live`,
which excludes Parakeet and Voxtral (both batch-only — `build_live_cmd`
has no backend for either) while the true-streaming Whisper families
(Whisper, NB-Whisper) and Moonshine light up.

Each `LiveChannel` declares a class attribute
`supports_native_vad: bool` so the dashboard (and the `/api/live/start`
boundary check) can refuse `gate_kind="backend"` against channels
that have no native VAD to defer to. `WhisperLiveKitChannel` is
True; the planned `ParakeetLiveChannel` will be False.

One tick's read of a channel for `/api/state` is a **`LiveSnapshot`**
(`live.py`): `info` copied, the log tailed to `LOG_PREVIEW_LINES` (a preview of
`LOG_TAIL_LINES`, the bound both channels give their `TailLog` and
`/api/live/log` serves in full), and `supports_native_vad` read with the same
safe-in-absence `False` that `speech_gate.effective_gate_config` uses — tolerant
because the flag is a required per-subclass declaration the base deliberately
omits, and a non-declaring channel must not 500 the ~2 Hz poll.
`live_control.plan_live` reads it strictly instead, because it genuinely
requires a conforming channel.

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

`MoonshineLiveChannel` (`tapscribe/moonshine_live.py`, PRD #120) is the
first `LiveChannel` besides WhisperLiveKit: a lightweight, low-latency
engine (Useful Sensors / Moonshine AI), English-only, MLX on Apple
Silicon or ONNX-CPU everywhere else. No subprocess — it's an in-process
`websockets.serve()` server exposing `/asr` that speaks `WlKRelay`'s
existing wire contract verbatim (the same cumulative `lines` snapshot
JSON WhisperLiveKit sends), backed by `MoonshineWindow`
(`tapscribe/transcribers/_moonshine_window.py`), a rolling-chunk
pseudo-streaming state machine that re-transcribes a growing buffer at
a short cadence and rolls over into a new line once a window would
exceed Moonshine's recommended sub-30s clip length. Consequence: the
Recorder, `SpeechGate`, and `LiveTranscripts` are unmodified —
`MoonshineLiveChannel` is a peer implementation of the same Protocol,
not a new pipeline. Two downstream pieces were *generalized* (not
forked) for it in #334: `WlKRelay.close()` sends the end-of-audio empty
binary frame and drains until `ready_to_stop` (the same wire signal
real WhisperLiveKit speaks) so close-time tail lines survive, and
`TapRelay`/`TapFanOut` hold the live channel through a resolver
(`lambda: recorder.live`) so already-open taps follow a family swap
mid-stream. `supports_native_vad = False` (no built-in VAC; TapScribe's
own `SpeechGate` is always the gate — a carried-forward
`gate_kind="backend"` is coerced to `"tapscribe"` at the tap's own gate
construction, never persisted into the operator's config).
Picking a Moonshine model swaps which concrete `LiveChannel`
`recorder.live` holds (`moonshine_live.resolve_live_channel_for_model`,
driven through the `live_control` reconcile seam that both
`/api/live/start` and the `AUTO_START_LIVE` boot path share — see below)
— the Recorder's own construction still always starts with
`WhisperLiveKitChannel`. The forward plan for Moonshine v2 "Voice"
(true incremental streaming) lives in PRD #120's Further Notes:
additive `ModelEntry` rows + a streaming channel variant when an MLX
port exists; the `LiveChannel` seam and the `/asr` snapshot contract
don't change.

### Live reconcile — the `live_control` seam

`/api/live/start` and the boot auto-start don't drive the channel
directly; both go through the FastAPI-free reconcile seam in
`tapscribe/live_control.py` so the swap-and-restart sequence lives in one
place instead of each re-deriving it:

- `plan_live(current, desired, *, use_mlx) -> LivePlan` is **pure**. It
  resolves the family swap (`resolve_live_channel_for_model`, run
  **unconditionally** — a persisted Moonshine default needs a swap even
  though `config.model` is unchanged, #259), validates the request (a
  *changed* model against the catalog allowlist; `gate_kind` against the
  **target** channel's `supports_native_vad`), and decides no-op /
  gate-knob-only / restart. It raises a `LiveReconcileError`
  (`LiveModelUnknown` / `GateKindUnsupported` → 400, `LiveStartFailed` →
  500, all registered in `routes.errors.DOMAIN_ERROR_STATUS`) **before touching
  anything**, so a rejected request leaves a running channel exactly as
  it was (#334 — the invariant is now structural, not ordering
  discipline in the route).
- `apply_live(current, plan, *, set_live)` runs the side effects (stop
  the old engine on a swap → `set_live(target)` → announce → restart),
  preserving the double-`begin_transition` around the teardown so
  `/api/state` stays on "starting" through the multi-second reload. It's
  offloaded to a worker thread.

The route parses the body into a `DesiredLiveState` (the parse-once test
surface) and is a thin shim; `set_live` keeps the slot owner
(route/lifespan) in control of `recorder.live`, so the ~38 other
`recorder.live` read sites are untouched. `plan_live` is unit-tested in
`tests/test_live_control.py` with no subprocess, engine, or TestClient.
This completes ADR-0003's `LiveChannel` seam — see ADR-0014 for why the
reconcile is free functions on the slot rather than a `LiveController`
object.

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
`quality_settings`).

`source_language` records the language the model was told to expect
(the ADR-0010 language pin; empty = the model auto-detected). TapScribe
does not translate (Canary was removed — see ADR-0006); the Canary-era
`target_lang`/`target_language` thread and the dashboard's translation
badge were deleted with it. Old sidecars carrying a `target_language`
key still load; the key is ignored.

Post-processors (currently just `hallucinations.apply`, possibly future
PII / phrase-replacement steps) consume a `TranscriptionResult` and
return a new one via `dataclasses.replace` — they never mutate in place.
After `hallucinations.apply` runs, `suppressed_hallucinations` holds the
dropped segments with their `matched_rule` annotated.

## Candidate languages · language pin

The operator's declaration of **which languages to expect** in a recording —
distinct from a model's catalog `languages` (what a model *can* transcribe) and
from `source_language` (what a model was told, or detected, for one WAV). It
exists because Danish and Norwegian Bokmål are near-identical, so per-WAV
auto-detect flips between them unpredictably; declaring the expected languages
removes the guess.

The set is attached **per meeting (Session), not per identity** — the same
speaker talks Danish in one meeting and English in the next, so language is
never a persistent property of a person. Within a meeting the set can be
**refined per tap**: a [single-person tap](#single-person-tap--multi-person-tap)
is narrowed, a [multi-person tap](#single-person-tap--multi-person-tap) keeps
the broader set.

One primitive, two behaviours:
- **Language pin** — a singleton set. Detection is skipped and the language is
  handed straight to the Transcriber. This is what actually defeats da/no
  confusion for a single-person tap.
- **Constrained auto-detect** — a multi-element set. Per-region detection
  still runs but its result is restricted to the set. Used for a multi-person
  tap, where no single speaker can be pinned.

Identity-level language is at most a **non-binding pre-fill** of the per-meeting
assignment, never the source of truth.

The operator-facing knob is the *languages*, not the model: a configurable
language→model map (a **generalist** = `batch-model.txt`, plus a **specialist
table** like `{no → nb-whisper}`) and a pluggable per-region **selector** turn
the set into transcripts. The default set is `{da, no, en}`. See ADR-0010.

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
- ActiveStream registration and the `bytes_received`/level updates —
  throttled to a flush every `STREAM_FLUSH_EVERY_FRAMES` (10) frames
  (~200 ms) rather than per frame, flushing unconditionally on each
  gate open/close transition and once at close (after the relay
  teardown, so the row reads the exact final byte count before it is
  removed) — plus the post-gate level meter and the gate-open
  transition push.
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

The wire contract is held **structurally**, not by review discipline, the
same way the tap-auth sweep above holds the auth gate: the Recorder's own
constants are the source, `tools/stamp_tap_wire.py` writes them into every
Bridge and every doc that restates them, and `tests/test_tap_wire_contract.py`
fails when any declaration drifts — including one nobody remembered to list.
See ADR-0019.

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

Bridges are distributed as GitHub-Release artifacts (built by CI on a tagged
release), downloadable straight from the dashboard's Settings "Get a bridge"
card — see [ADR-0012](docs/adr/0012-bridge-artifacts-on-tagged-releases.md).

The mnemonic: **TapScribe** = Bridge (the Tap) + Recorder (the Scribe).

## Tray Bridge

The native tray-icon Bridge family: one cross-platform **bridge core**
(the meeting bracket, CaptureOrchestrator, Level gate, and the `/tap` +
control clients) plus a thin per-OS **shell** that contributes only
audio capture, device enumeration, storage, and the tray UI. The
Windows tray Bridge came first; the macOS tray Bridge is a sibling
shell over the same core (ADR-0020 records the macOS platform
choices). Unqualified "the tray Bridge" means the family; qualify with
the OS only when the shell matters.

_Avoid_: "the tray app" (it is a Bridge — it taps audio into a
Recorder; a Bundle is what *is* a Recorder), and "the Mac bridge" for
the family (that names one shell).

## Bundle · Launcher

A **Bundle** is a self-contained, platform-native distribution of the
Recorder: an embedded CPython, the `tapscribe` wheel, and a Launcher,
installed per-user with no Python prerequisite. The Windows Bundle
(`TapScribe-Setup-win-x64.exe`) is the first; the name generalises to a
future macOS or Linux equivalent without re-coining.

A **Launcher** is the small executable *inside* a Bundle that points
`TAPSCRIBE_BASE_DIR` at the operator's data directory, boots the
Recorder, and opens the dashboard. It is not the server — it starts one.

A Bundle is **not a Bridge**. A Bridge taps audio *into* a Recorder; a
Bundle *is* a Recorder, packaged. Consequently a Bundle never appears in
`bridges_catalog.BRIDGE_ARTIFACTS` or the dashboard's "Get a bridge"
card — you need a Bundle to have a dashboard, so a dashboard cannot
advertise one. Bundles are announced by the README and the GitHub
Release page; they still ride ADR-0012's mechanism of CI-built assets
attached to a tagged release under stable, unversioned filenames.

_Avoid_: "installer" for the artifact (it names the act, leaving no word
for the installed result), "the exe", "the Windows app".

## HTTP auth gate · auth schemes

The single FastAPI middleware (`auth.basic_auth_middleware`) that decides,
for every HTTP request, which of **three auth schemes** applies. It is the
ONE place the disposition is chosen, so the schemes can't drift apart:

- **Public** — exact `(method, path)` in `config.AUTH_EXEMPT_ROUTES`
  (`/health`, `/healthz`). No credential.
- **Tap-bearer** — any path under `config.TAP_PREFIX` (`/api/tap`). The
  Bridge's HTTP control plane (`/api/tap/new-session`,
  `/api/tap/sessions/{session}/pipeline`): authenticated by the tap token as
  `Authorization: Bearer` (`auth.check_tap_bearer`, constant-time), NOT
  dashboard Basic auth. The SAME middleware branch that routes these
  requests past Basic also **enforces** the bearer — exempt-from-Basic and
  requires-bearer are one predicate, so a new `/api/tap/*` route is gated
  **by construction** (it cannot be added un-gated). Handlers carry no gate
  of their own.
- **Basic** — everything else: dashboard HTTP Basic against
  `recorder.auth.value`.

The `/tap` **WebSocket** is a fourth, separate path: middlewares of this
kind don't see WS upgrades, so it carries the tap token in
`Sec-WebSocket-Protocol` and is gated by `auth.pick_tap_subprotocol` from
the WS route handler (see [Bridge](#bridge)). The two tap mechanisms share
the `recorder.tap.value` secret but never the transport — so when precision
matters, say "the tap-bearer scheme" (HTTP) vs. "the `/tap` subprotocol
gate" (WS), not "tap auth."

The invariant is held structurally, not by review discipline: a
route-discovery sweep (`tests/test_tap_endpoint.py`) enumerates every
registered route under `TAP_PREFIX` and asserts each rejects a
missing/wrong bearer, so the gate holds for future routes without a new
test. See ADR-0008.

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

## Single-person tap · Multi-person tap

A property of each `/tap`: does the audio on this WS carry **one** human or
**several**? This is the canonical language-and-attribution axis — it replaces
the ad-hoc "mic vs. system" / "loopback" framing. Say "multi-person tap," never
"loopback."

- **Single-person tap** — one human for the whole tap (a SpatialChat
  per-participant tap; the tray Bridge's microphone = the operator). Its
  [candidate languages](#candidate-languages--language-pin) can be narrowed to a
  **pin**, and live-caption attribution is exact.
- **Multi-person tap** — several humans mixed into one stream (the tray
  Bridge's system-audio capture — the "them" side of a meeting). No single
  language to pin, so each region resolves language on its own (constrained
  auto-detect, or the generalist + Norwegian-specialist selector), and *who*
  said each part is unknown until **diarization (#78)** lands. The multi-person
  tap is the single locus where both per-region language selection AND future
  diarization live; they compose — diarization splits a region by speaker, and
  language resolves per resulting sub-segment.

This **nuances** the "One `/tap` WS = one speaker at a time" invariant: what a
WS guarantees is one **attribution identity** (one caption bucket), not
literally one human. A multi-person tap is one identity that diarization will
later split into humans; until then its identity (e.g. the tray's `system`) is
the coarse "me vs. them" bucket CaptureOrchestrator already draws.

## Person · Identity · Roster · People Registry

The canonical, cross-session naming model (ADR-0009). Distinct concepts that
are easy to conflate:

- **Identity** — the bridge-stamped token per device/participant, stored
  **untruncated**. Stable across sessions for our bridges (SpatialChat:
  `participant.identity` = the account `user.id`, constant across meetings;
  Windows-tray: per-device; local-test: OS user). The cross-session join key.
  Note the WAV filename still carries only `safe_name(identity)[:10]`
  (`parse_wav_speaker_ident`) — *truncated*, so the filename slug is **not** a
  reliable key; the full Identity comes from the Roster.
- **Occurrence** — one appearance of an Identity in one session (live or
  recorded). Its **speaker key** is `identity` today; once diarization (#78)
  splits one Identity into several voices it becomes `identity#cluster`. Auto
  recognition only joins stable Identities across sessions — diarized clusters
  are session-local (Monday's `speaker_0` ≠ Tuesday's) and need manual merge.
  This leaves the [one-`/tap`-WS-=-one-speaker invariant](#invariants) intact.
- **Roster** — a per-session, machine-written sidecar (`session-roster.json`)
  mapping `full identity → { name, source, wav refs }`, written by the tap path
  at open/close. Separate from the operator-editable `session_meta.json` and
  from the per-identity `tap_settings` gate record (ADR-0007). It is what makes
  the full Identity recoverable for recorded occurrences.
- **Person** — a global entity: one display name + a set of member Identities.
  Every new Identity **auto-binds** to its own Person (default-named from the
  bridge `name`), so the registry is never empty. **Merge** combines two
  Persons (survivor's name wins, Identities join — like `absorb_session`);
  **detach** pulls one Identity back out as undo.
- **People Registry** — the single global store (`people.json`, recordings
  root) and **source of truth** for names. The server resolves
  `identity → Person → name` when building `/api/state` and the merged
  transcript, shipping the same name-map shape the frontend already renders, so
  the [Interaction hold](#interaction-hold) render path is untouched.
- **Override** — the demoted role of `session_meta.aliases`: a per-session
  alias that beats the Person's global name *for that session only*.

Resolution precedence (server-side): **Override** › **Person** name (via
Identity membership) › bridge display name / slug fallback. Rosterless old
sessions resolve by slug and keep rendering — no regression.

## Utterance

A continuous speech segment from one speaker, delimited by mute
boundaries on the Bridge side. One utterance maps to one WAV on disk
and — most of the time — to one `/tap` WebSocket. When the network
blips mid-utterance, the Bridge reconnects with the same `utterance_id`
and the Recorder appends to the existing WAV rather than starting a new
one (see `UtteranceIndex` in `tapscribe/recorder.py`), so an utterance is
a logical concept that can span multiple physical `/tap` WSes.

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

A Windows **capture** endpoint (a mic) *does* have a native mute — the
endpoint's `AudioEndpointVolume.Mute` — and the bridge honours it as a
**hard gate-closed** (#159): while the mic is muted it produces no taps at
all, independent of level, and an open utterance closes the instant the
mic mutes. This is necessary because a muted mic keeps delivering a
residual (noise floor / DC offset / device blips) that the Level gate
alone would occasionally tap as a recurring "quiet" utterance. The native
mute and the Level gate compose: muted is an authoritative override above
the gate, so the gate stays a pure level function and only governs the
unmuted stream. A **render** (loopback) endpoint has no such mute, so
there the Level gate remains the only Mute. The mute capability is
surfaced through the capture seam itself (`IAudioCapture.IsMuted` /
`MuteChanged`), so a future macOS/Linux mic backend honours OS mute the
same way without the bridge core knowing the platform.

## Level gate

The Bridge-side RMS **level** gate that decides utterance boundaries on
platforms that have no native mute event — it is how the Windows tray
Bridge produces **Mute**. Lives in the cross-platform bridge core
(`GateOptions` / `LevelGate` under `bridges/windows-tray-bridge/`); its
knobs are the **open threshold** (a linear RMS amplitude), the
**hangover** (silence-to-close), and the **pre-roll** (leading audio
replayed when the gate opens so the first consonants aren't clipped).

Tuning is **per device**, not global (ADR-0007): each capture device
carries its own `GateSettings` (the operator-unit `sensitivity` slider +
hangover/pre-roll, in `Bridge.Core`), so the system-loopback gate can be
more sensitive than the mic gate. The per-device tuning rides on the
device selection, reaches each pipeline's `LevelGate` at Start, and is
re-tuned live by **identity** (`CaptureOrchestrator.UpdateGates(map)`) on
Settings → Save — only the changed device's pipeline re-tunes; a device
with no running pipeline is skipped. The legacy single global gate is kept
only as a nullable migration input: an old file's one tuning loads as each
device's default (no reset on upgrade).

Distinct from the Recorder-side **SpeechGate**: that one is Silero-backed
and its threshold is a speech *probability*; the Level gate is amplitude
RMS. The two gates live on opposite sides of the `/tap` wire and **never
share threshold units** — UI that surfaces the Level gate must not borrow
SpeechGate's "speech threshold" vocabulary.

### Input-level meter

The Level gate's threshold is invisible while tuning, which is what made
the quiet-system-audio bug hard to diagnose. The Settings dialog's
**input-level meter** (per device, #152) closes that gap: a live RMS bar on
the **same** scale as the gate threshold — `AudioLevel.Rms` is the one
reading the gate and the meter both use, and `LevelMeterScale` is the shared
log axis the sensitivity slider rides — with the device's threshold drawn as
a marker, so "is my voice / the meeting audio above the line?" is answerable
at a glance. It is **display only**: a second, throwaway shared-mode capture
(`InputLevelMeter`) that never touches the tap/gate pipeline. Don't conflate
it with the Recorder-side post-gate **level meter** (under ActiveStream
above) — opposite side of the `/tap` wire, and this one is a *pre-gate
tuning aid*, not a readout of what's being recorded.

## Drain

The flush of trailing PCM that was buffered on the Bridge when mute fired
but hadn't yet been delivered to the Recorder. On mute, the Bridge keeps
its reconnect ladder running for up to `DRAIN_MAX_MS` (8 s); once a `/tap`
WS reconnects, the buffered audio is flushed to it before the WS closes,
so the WAV finalizes with that trailing audio intact. The timeout exists
so an unreachable Recorder can't wedge the utterance forever. Implemented
in the Bridge's `startDrainTimer()` / `endUtterance()` and on the Recorder
side in `live_relay.close()` / `_flush_tail()`.

## Blip-resilience recipe

The concrete numbers a Bridge uses to survive a transient `/tap` failure
without losing audio: the reconnect **backoff ladder** (with jitter), the
**gap-buffer** cap on PCM held while disconnected, and the [Drain](#drain)
budget. Named as one thing because the three only make sense together — a
ladder without a buffer reconnects to nothing, a buffer without a bounded
drain wedges the Utterance.

Unlike the [Wire contract](#bridge), these are **recommended, not
enforced**: the Recorder has no opinion on them, and a third-party Bridge
may deviate deliberately. What is not allowed is the two bundled Bridges
and the docs drifting from *each other*. (Prefer this term over the older
"recommended defaults" / "reference recipe" wordings, and don't say
"reference recipe" at all — "reference implementation" already means the
local-test Bridge.)

The **first-connect-failure semantics** are pointedly NOT part of the
recipe: whether a failure on an Utterance's very first connect is terminal
or retried is an open per-Bridge choice, and the two bundled Bridges answer
it differently on purpose. See `bridges/README.md`.

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
re-rendered) while a control inside it is focused, a popover or
`<dialog>` inside it is open, a text selection touches it, or — for
tail-following panels — the operator has scrolled away from the tail.
Deferral always skips **without advancing the render gate**, so the held
render lands on the first tick after the interaction clears; updates are
delayed by the operator's own interaction, never lost.

A held render lands exactly one way: the **tick-retry**. The deferral
marks a flag (`markDeferredRender`), and the next poll pass consumes it
(`consumeDeferredRender` in `next/main.js`) and re-derives the render
from live state — which is why the flag has to survive an unchanged
`/api/state`, since the server going quiet is not the operator letting
go. There is no second mechanism, and adding one is a decision, not a
detail: ADR-0016 records why the alternative (a per-host listener
replaying a captured build the instant the hold clears) fits only one of
the four render shapes and cost two hold registries per host.

Mechanics live in `web/js/templates.js`: `renderRegion(host, build, {sig})`
is the swap-based primitive (it owns the per-host signature and all three
holds, and checks the signature FIRST so an idle focused control marks no
retry), `selectionInside(host)` is the shared selection predicate, and
`markRegionStale(host)` invalidates a host's remembered signature so the NEXT
`renderRegion` re-renders after a mutate / lazy-body load — the "defer, don't
force" reset. There is no force flag: a render that skips the guards is never
what a call site wants. Swap-based copy-target panes render through
`renderRegion` (the Summary output pane, the Transcript merged pane, plus the
spine/settings/live-channel/config-card regions). The decision and its rejected
alternatives (DOM-diffing, capture-and-restore, pausing the poll) are
ADR-0004. Say "this region needs the interaction hold," not ad-hoc
descriptions of focus/selection guards.

### Region · keyed list

The two shapes a held render takes, and the words for them:

- A **region** is rendered by being **swapped whole** — `renderRegion`
  `replaceChildren`s a freshly built subtree in.
- A **keyed list** has its rows **created once, matched by key, and updated in
  place**, and is never swapped. `renderList(host, items, {key, create, update,
  itemSig, sig})` is its primitive; `markListStale(host)` is its
  `markRegionStale`. The host's children belong to the seam alone, so a keyed
  list's empty/loading **placeholder is a SIBLING** of the host, toggled in
  place — never swapped into it.

Both need the interaction hold; `renderList` additionally owns two rules a
region has no equivalent for. **The removal hold**: a row holding a focused
control whose key has left `items` is **retained** — spliced back at its
position, so it stays on screen (and keeps its focus) while every other row
updates, and drops on a one-shot `focusout`. "Never destroy interaction state"
applies most to removal. Note a retained row is on screen but NOT in `items`, so
a caller narrating the list is describing `items`, not the DOM. And **the per-row
hold**: a focused control freezes its own row's `update` and leaves that row's
`itemSig` unstamped. That hold is deliberately coarser than a per-control guard,
matching a region holding whole.

Which hold fires is a two-predicate question, and the distinction is the point:
removal destroys focus on anything **focusable** (including a scripted
`role="button"`), while an in-place write only threatens **editable** state — a
value, a caret, an open dropdown. So the removal hold takes the wide predicate
and the write holds take the narrow one. A **raw swap** is neither: it detaches
everything under the host, so it takes the full hold via
`deferIfInteractionInside` (focus or selection), not `deferIfSelectionInside`,
which covers only half.

A call site uses whichever change detector it has, and may use both or
neither: a list-level `sig` pays only where a **cheap aggregate stamp** already
exists (`files_sig` for the WAV lists), while `itemSig` is the answer where one
doesn't (the Sessions rows change independently and no server digest covers
them, so building a list-level sig would itself be the O(rows) walk the gate is
meant to skip).

Say "this is a keyed list" / "it needs the removal hold", not "the list gate".

## Pending edit

An inline edit the operator has typed but the server has not confirmed:
held in an **optimistic overlay** (id → value) so the field, and every
other region showing the same value, reads the typed text immediately
instead of snapping back to the server's on the next poll tick. Distinct
from the render canon's "overlay" (a popover/`<dialog>`) — say "pending
edit" or "optimistic overlay", never bare "overlay".

A pending edit is saved by a **field saver**
(`web/js/next/field-saver.js`): one debounced PUT per id. It is cleared
by the **catch-up sweep**, which drops every pending edit the server has
caught up to, once per tick, before anything computes a render
signature — without it a stale pending edit masks a later change made
elsewhere forever. Deleting a pending edit is also the ONLY way to
cancel its save: the saver re-reads the overlay when its timer fires.

A pending edit **pins its row into a filtered list** until the save settles —
`sessions.js`'s filter returns true for any session with an unsettled rename.
This is NOT the mid-edit case: protecting the node being typed in is the
[interaction hold](#interaction-hold)'s job and keys on FOCUS. The pin keys on the
unsettled SAVE, covering the window after blur where the row would otherwise
leave and take with it the status cell a `failed: …` must land in — so a failed
rename would report itself into nothing and the operator would never learn it
did not stick. A filter predicate that reads save state is the shallow fix; the
deep one is a fallback in the save-status lifecycle for a status with no live
cell, which would retire the pin.

Every save that reports itself — a field saver's, and a save BUTTON's —
narrates into **status cells** through the one **save-status lifecycle**
(`web/js/save-status.js`): `saving…`, then `saved` (auto-clearing) or
`failed: <reason>` (which stays until superseded, so the operator can
read it). Say "wire it through the save-status lifecycle", not
"set the status text".

Each editable resource has ONE owner module binding those pieces
together — `next/session-labels.js` for session labels (shared by the
Sessions list and the spine's rename card, which PUT the same endpoint);
People names are owned by `people.js`, whose editor is the only one.
Say "this needs a field saver" / "the catch-up sweep dropped it", and add
a new editor by writing an owner, not another copy of the machine (#355).

## Lazy resource · resolve · failure policy · last-good hold

A **lazy resource** is one body the dashboard fetches on demand instead of
carrying on the ~0.5 s `/api/state` poll, cached under a **signature key**
that changes exactly when the body does: the merged transcript keyed on
`(session, transcribed_at)`, the persisted summary on `(session,
summarized_at)`, the per-session WAV listing on `(session, files_sig)`,
waveform peaks on `(session, wav, source, byte size)`. A re-transcribe /
re-strip / new WAV flips the signature and busts the key; an idle poll
reuses the cached promise and fires no request. The mechanism is
`web/js/lazy-resource.js`; `web/js/api.js` declares each resource's URL,
key and policy.
_Avoid_: "the transcript cache", "the files fetch" (they are the same
thing with different payloads, and treating them as different is what
produced five copies of the machinery — #222).

A consumer that wants one **watches** it — `resource.watch(onLand)`, once,
at build time — and then **resolves** through that watcher every render
tick: `body.resolve([...key args])`. The resolve owns everything that used
to be re-derived per call site: fetch the key ONCE (deduped across the
ticks before it lands, and across *watchers* — two views waiting on the
same key share the fetch and each get their own land), apply the failure
policy, and answer with `{ value, loading, stale, error }`. `loading` is
true only on a genuine **cold load**, which is the only state a caller may
paint a placeholder for; **`stale`** says the value is the last-good body
rather than this key's own, and a render gate keyed on the signature must
carry it as a term (held rows and that signature's own rows are otherwise
the same sig, so the swap between them would be skipped) — spelled once by
whoever owns the resolve, never per view. `onLand` fires
when there is something new to show and is where the view drops its render
gates (`markRegionStale` / `markListStale`) and repaints — it is the prompt
repaint, not the thing correctness rests on. One resource has many
watchers; each keeps its OWN failure memory, so the watcher that pays for a
skipped retry is the one that fires it, and a view REBUILD starts from
nothing remembered (which is what lets a transient boot-time failure under
`remember-error` be retried at all). A watcher
belongs to one consumer for its whole life, which is what makes the
identity-dedupe of waiting callbacks something the seam guarantees rather
than something each call site has to remember.
_Avoid_: "the pending set", "the rerender-pending map" — that bookkeeping
is the resource's, not a view's. Don't say "pass a callback to resolve":
the callback belongs to the watcher.

A resource's **failure policy** is declared once, at the resource, and is
a decision about the body rather than about the code around it:
`retry-next-poll` (a rejection is silent and the key retries on a LATER
tick — a rejection evicts the cache key, so the *pacing* is the point:
without it the failure's own repaint re-resolves and refires the fetch at
HTTP-response rate) or `remember-error` (report the rejection and stop
asking until the signature changes — an unreadable WAV has no peaks, so
re-asking every tick answers nothing and the operator wants the reason).

The **last-good hold** (`holdKeyOf`) is stale-while-revalidate memory,
keyed by what the body BELONGS to (the session) rather than which version
it is. It is required when the signature is a session-level aggregate that
flips when any one sibling changes — `files_sig` flips once per track
during a batch transcribe — because otherwise every flip reports a cold
load and the region blanks to "loading…" once per sibling (#266). Omit it
when a stale body would be *wrong* rather than merely old.
_Avoid_: "the stale cache" (it is not a cache — it holds exactly one value
per session and exists only to keep a region from blanking).

## Player · seek target · open WAV · playhead

The dashboard's audio playback. Playback belongs to the operator's
**session work**, not to a view: there is exactly ONE **Player** — a single
audio element owned by the shell and mounted outside `#viewRoot`, never
inside a region, a keyed-list row or a view host, all three of which are
detached or swapped out from under a playing element. Views own only
**play affordances** (a per-WAV control in Recordings, a per-line control
in Transcript) that hand the Player a seek target; a view that mounts its
own element is the bug this term exists to prevent. The Player is driven by
media events, NOT by the poll, so it sits outside the render-signature and
interaction-hold machinery entirely.
_Avoid_: audio widget, per-view player, transport.

A **seek target** is what a play affordance hands the Player: the triple
(`source_wav`, source, `offset_s`) naming the exact file the audio is in and
where in it to land. Every merged segment already carries `source_wav`, so a
transcript line plays the file its words actually came from — a stripped
clip when the session was transcribed from stripped audio, the original
otherwise — and the offset is `abs_start` minus that file's `wav_start`. A
segment is never mapped onto a *different* file's timeline.
_Avoid_: timestamp jump, seeking to a time (a time alone is not a target —
concurrent per-speaker WAVs make session-relative time ambiguous).

**Every verb that removes audio must tell the Player** — `player.forget(file)`
for one WAV, `player.forgetWhere(pred)` for the bulk ones (clear stripped,
re-strip, delete a session's audio, delete a session). This is a convention, not
something the seam can enforce: each caller knows *which* files went and nothing
downstream can derive that. Skipping it fails **silently and alarmingly** — the
browser has the bytes buffered, so a deleted recording keeps playing to the end
with no media error at all. Say "that delete path needs to evict the Player".

The **playhead** is the Player's position drawn on the Recordings waveform.
It is shown ONLY while the file the Player has loaded is exactly the file the
canvas is displaying — never projected onto another file's timeline, for the
same reason a seek target names a file. Clicking the waveform is itself a
seek target on the displayed WAV. Both directions make the canvas a control
surface as well as a display one; the cut overlay and the peaks remain
display only.
_Avoid_: cursor, position marker, scrubber (the scrubber is the Player's own
native control).

An **open WAV** is one a tap is writing right now. It is **not playable**:
the RIFF/data-size header is patched only when the tap closes, so the bytes
on disk declare a length that isn't there yet. Open-ness is already what
`files_sig` masks (a growing size would flip it ~2 Hz); the same set drives
the affordance, so playability turns itself on exactly once, when the tap
closes and the listing refetches.
_Avoid_: live WAV, current WAV (the *current session* is a different thing).

## Source pick · original / stripped · effective source

Which audio of a session the dashboard acts on: the **originals** as recorded,
or the **stripped** region clips a strip-silence run wrote. The **source pick**
belongs to the SESSION, not to a view — it is *that session's* source — so
Recordings and Transcript read and write ONE store owned by the Stages shell
(`next/shell.js`, keyed by session id, in-memory only: a reload comes back on
the original, the pick is deliberately not persisted). Picking "stripped" in
either stage is the pick the other stage lists, transcribes and plays from. A
view keeping its own copy is the bug this term exists to prevent (#354).
_Avoid_: source toggle (that is the CONTROL that writes the pick), per-view
source, audio mode.

The **effective source** is what every consumer must act on:
`effectiveSource(session)` falls back to "original" whenever the pick says
"stripped" but the session has no `stripped/` folder, so a stale pick can't
operate on nothing after the clips were cleared. Resolved per tick from the
store — a view that caches the resolved value paints a source the session left.

**A source switch must also drop the switching view's derived artifacts** —
Recordings' live strip-preview is tuned against the ORIGINAL, so a flip to
stripped has to clear it or it stays overlaid on the committed cut. The store
has no subscribers, so this cannot live at the write site: each view reconciles
the source it last painted (recordings' `lastSrc`) and invalidates its own
derived state, which is what makes the invariant hold for a pick made in the
OTHER stage. Same class of convention as "every verb that removes audio must
tell the Player": the seam cannot enforce it and it fails silently.

**Every verb that removes a session's stripped clips must clear the pick** —
`clearSourcePick(session)` on clear-stripped, delete-audio and delete-session.
`effectiveSource` masks a stale "stripped" only while the folder is gone; the
session can regain one (a later strip, an absorb) and the un-cleared pick would
then govern the stages with no operator having picked it.

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

## Route module · State view

A **route module** is one file under `tapscribe/routes/` holding the routes of
one resource group, with a complete route map in its docstring. Modules are
grouped by domain concern rather than URL prefix, so `strip` owns four routes
spanning two prefixes and `tap` keeps each auth twin beside its sibling
(ADR-0018). `routes/__init__.py` is the index: read it to find where something is
served. `app.py` holds no routes; it is app construction, middleware, the
domain-error registry and the includes.

Four support modules are the only thing a route module may import from a sibling:
`deps` (the shared `get_recorder` dependency), `body` (read a JSON body, parse one
field of it), `errors` (domain error to HTTP status) and `guards` (the
destructive-route preflight: refuse the current session, a busy session, a session
with a live tap, and a session with an in-flight tap mark from `tap_registry`).

The **State view** (`tapscribe/state_view.py`) is the read model behind
`GET /api/state`: given one **`StateInputs`** — a frozen record of everything
recorder-owned at one instant — it produces the poll payload and its ETag.
Distinct from the `sessions` read model below, which supplies the session listing
the view joins in. Request-free and Recorder-free, so the projection, the
per-session default-override counts and the open-tap byte bucketing are testable
without a route.

`StateInputs` is the whole interface — one value object, not the thirteen
keyword arguments the route used to marshal — so a tick can be built by hand and
nothing the worker thread touches is still being mutated. Two of its fields carry
the seam's remaining rules. `live_identities` is a derived PROPERTY over
`active`, not a field: the People join must see exactly the identities with an
open tap, and passing both left "these agree" as a docstring promise; the route
derives its own copy through the same `live_identities_of` before the registry
mutation runs. The live channel arrives as a **`LiveSnapshot`** (`live.py`,
beside `TailLog`), which owns the `info` copy, the `LOG_PREVIEW_LINES` log tail
and `supports_native_vad`'s safe-in-absence `False`, so the route no longer knows
how much log a poll ships. `live_feed` stays its own field: those settled lines
come from the Recorder's `LiveTranscripts`, a different owner, and a snapshot
with two owners could not be captured in one call.

## Session modules — paths · listing · maintenance

Recording-session bookkeeping is split across four modules by concern, so the
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
  reads/writes meta via `sessions`. Reads the in-flight tap mark through
  `tap_registry` in `prune_empty_sessions` (the point that enforces the
  prune-vs-tap invariant, #257), and re-exports the registry's names so #257's
  leak detector and the destructive-route contract keep reaching them here
  (#405).
- **`tap_registry`**: the canonical home of the in-flight tap mark
  (`mark_session_in_flight` / `release_session_mark` / `session_has_open_tap`),
  a session-dirname refcount that `TapFanOut._open` takes before its session
  mkdir and holds until the tap closes. A stdlib-only leaf that imports nothing
  in TapScribe, so both readers (the recording hot path and the
  destructive-route preflight in `routes/guards`) reach it without pulling
  operator-maintenance weight (#405). Not on the Recorder: the mark is not
  recorder state (#257).

  The destruction guard (`register_tap` / `unregister_tap` /
  `try_claim_destruct` / `release_destruct`) is a second primitive in the same
  module: a per-session reader count + `threading.Lock` that the threaded
  destructive routes use to atomically check for open taps before their
  filesystem walk (`try_claim_destruct`), and that `TapFanOut._open` increments
  (`register_tap`) so the worker sees a live tap. A tap arriving while
  destruction is in progress is refused by the ``-1`` sentinel.

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
  operator gets fast feedback on noise files, then runs the meeting's
  **cover** over that single WAV (the generalist plus any specialist for
  the meeting's candidate languages — the same routing as the range,
  ADR-0011), points `_primary` at the selector's winner, and returns the
  winning sidecar's raw JSON dict. `force=True` — an explicit per-WAV
  request bypasses the cache. Brackets its work in `recorder.jobs.run`
  (`kind="transcribe"`), so a manual per-WAV re-transcribe gets
  `SessionBusy` (409) while ANY other job holds that session — a range
  transcribe, a strip, or an end-of-meeting pipeline. Without that claim
  two covers ran concurrently, doubling resident models and racing
  `set_primary_transcript` on the same WAV.
- `transcribe_session(recorder, BatchSessionRequest) -> dict` — every
  WAV in the supplied `from_iso`/`to_iso` range. Brackets the loop in
  `recorder.jobs.run` too (the Session job seam — both entry points claim
  it), reporting progress through the
  yielded handle, then merges via `merge_session` and writes both
  `session-transcript.json` and `.txt`. No per-WAV silence pre-check —
  the session loop transcribes everything in range.

Both forms share a `TranscriberInvocation` envelope (initial_prompt,
hotwords, source_lang, candidate_languages, hallucination_rules)
resolved once per request. The prompt/hotwords resolution layers session-meta over
the global config files (`config/prompt.txt`, `config/hotwords.txt`);
an empty session-meta override falls back to the global default.

The module never raises `HTTPException`. It raises domain errors — its own
`WavTooQuiet` / `WavUnreadable` (under `BatchTranscribeError`), plus
`SessionBusy` (from `recorder`, via `jobs.run`) and `NoUsableWavs` /
`InvalidRange` (selection verdicts, from `session_merge`). A single
domain-error handler registered from `routes/errors.py` maps each error type to its HTTP
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
returns the summary dict. The summary persists to `session-summary.json`
(#83) and the operator's default source/command/model/prompt persist to
`config/summarizer.json` (#84, `api_key` write-only); a per-session
`session_meta` override still beats both, per
`effective_summarizer_config`.

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
  allowlist (`is_allowed_local_model`), per-machine hardware routing, and the
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

`ApiSummarizer` (OpenAI-compatible / Ollama, #85) is shipped: POSTs
`{base_url}/chat/completions` over stdlib urllib, `base_url`/`model` are
config, `api_key` is write-only (never echoed back, only `key_set`). Its
model list is the one thing still pending — the `api` source takes a free-text
model, not a catalog pick like `local`. Adapter-level errors are
`SummarizerUnavailable` (misconfigured / not-wired source → 400) and
`SummarizerFailed` (ran but failed → 502).

## Wire-format note

Result JSON files (per-WAV `<wav>.transcripts/<...>.json` or legacy
`<wav>.json`, plus `session-transcript.json`) use
`"transcriber": "faster-whisper" | "mlx-whisper" | "voxtral"`. This is a
rename from a prior `"backend"` field; older recordings written before
the rename may still use the old key.
