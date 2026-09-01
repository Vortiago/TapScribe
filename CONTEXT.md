# TapScribe — domain glossary

Canonical names for the concepts that show up across the codebase. When in
doubt, prefer these over synonyms; don't introduce shadow vocabulary in code
or docs.

Definitions only — what a term IS. Mechanism lives in CLAUDE.md, rationale in
`docs/adr/`, and each entry points at its own.

## Recorder

The TapScribe Python application: FastAPI server, the `/tap` WebSocket handler,
session bookkeeping, the dashboard, and the supervisor for both live and batch
Transcribers. One instance per process (`tapscribe/recorder.py`).

Verb/noun split: **the Recorder** writes **a recording**, one WAV per
[Utterance](#utterance) per speaker. `recording_enabled` gates new recordings
only; live transcription is independent of it.

## Transcriber

The protocol for "something that can transcribe one WAV":
`transcribe(path, *, initial_prompt, hotwords, source_lang) -> TranscriptionResult`.
Each declares a `device`, a `name` (the family — `"parakeet"`) and a `backend`
(the runtime that did the work — `"parakeet-hf"`). Adapters live in
`tapscribe/transcribers/`; the catalog is the
[TranscriberRegistry](#transcriberregistry).
_Avoid_: "the live Transcriber" — a
[LiveChannel](#livechannel--whisperlivekitchannel--moonshinelivechannel) is not
one. Say "the live channel".

## TranscriberRegistry

The one declarative source of truth for every model TapScribe knows about
(`transcribers/catalog.py`). Each `ModelEntry` declares the model's id, family,
languages, the contexts (`batch` / `live`) whose picker may offer it, its
backend bindings and its [ModelInput](#modelinput--textinput)s.

Adding a model is one new entry; the registry is also what `load_transcriber`
resolves against, so an unlisted id cannot be loaded.

## BackendKind / BackendPreference / available_backends

`BackendKind` is the concrete runtime a model resolves to (`mlx` / `cuda` /
`cpu`). `BackendPreference` is what the *operator* picks, adding `auto`, which
resolves at call time to the first kind both available here and supported by
that model. `available_backends()` is the cached probe of what this machine
has.
_Avoid_: conflating these with `install_picker`'s `BackendDef` strings — those
say which pyproject extras to install, not what the runtime selects.

## ModelInput — TextInput

A per-model UI form-field declaration the registry attaches to a `ModelEntry`;
the dashboard renders it and the transcribe routes forward only the values the
registry says the adapter accepts. `TextInput` is the only kind today, so
`ModelInput` is a degenerate union.

An adapter that ignores an input still echoes it into the result's audit
fields (`initial_prompt_used`, `hotwords_used`, `source_language`).

## LiveChannel · WhisperLiveKitChannel · MoonshineLiveChannel

`LiveChannel` is the Protocol for a streaming-caption engine (ADR-0003); the
Recorder holds exactly one instance and never names a concrete class.
`WhisperLiveKitChannel` supervises the `whisperlivekit-server` child process;
`MoonshineLiveChannel` is an in-process server speaking the same wire contract.
Both inherit `LiveChannelBase`, which owns the shared transition surface; a
from-scratch implementer may satisfy the Protocol directly. Rationale:
ADR-0014.

Two per-engine capability flags gate operator choices: `supports_native_vad`
(false ⇒ `gate_kind="backend"` is refused, since there is no native VAD to
defer to) and `fixed_language` (a single language ⇒ a language change never
forces a restart).

`info["device"]` is an **observation, not an assertion**: the parent cannot pin
the child's device, so the label is a prediction overwritten by what the child
reports. A new adapter must keep that semantic.

### Live reconcile — the `live_control` seam

The FastAPI-free seam (`tapscribe/live_control.py`) both `/api/live/start` and
the boot auto-start drive the channel through, so the swap-and-restart sequence
has one owner. `plan_live` is pure and validates before touching anything, so a
rejected request leaves a running channel exactly as it was; `apply_live` runs
the side effects on a worker thread. Rationale: ADR-0014.

## SpeechGate · gate_kind

The per-`/tap` Silero-backed speech gate between
[TapFanOut](#tapfanout)'s frame write and the live relay, holding a pre-roll
ring buffer so an utterance's leading consonants survive.

`gate_kind` is the operator's choice of which gate decides speech:
`"tapscribe"` (the default — this gate, with the backend's own VAD off) or
`"backend"` (defer to the engine's).
_Avoid_: sharing threshold vocabulary with the [Level gate](#level-gate) — this
one's threshold is a speech *probability*, that one's is amplitude RMS.

## TranscriptionResult

The frozen return value of `Transcriber.transcribe(...)`: the segments, the
joined text, which transcriber/model/device produced it, and the inputs that
were in effect. `source_language` records what the model was *told* to expect
(empty = it auto-detected).

Post-processors (`hallucinations.apply`) consume one and return a new one via
`dataclasses.replace`, never mutating in place.

## Candidate languages · language pin

The operator's declaration of **which languages to expect** in a recording —
distinct from a model's catalog `languages` (what it *can* transcribe) and from
`source_language` (what one WAV was told, or detected). It exists because
Danish and Norwegian Bokmål are near-identical, so per-WAV auto-detect flips
between them unpredictably.

The set attaches **per meeting, not per identity** — the same speaker talks
Danish in one meeting and English in the next — and may be refined per tap. One
primitive, two behaviours: a singleton set is a **language pin** (detection is
skipped), a multi-element set is **constrained auto-detect** (detection runs but
its answer is restricted to the set). ADR-0010.

## LiveChannel · ActiveStreams · LiveTranscripts

The three internal dataflows the Recorder fans one `/tap` WebSocket out into,
one per dashboard panel: the live channel (bytes relayed for captions),
ActiveStreams (the open taps writing WAVs), and LiveTranscripts (a bounded
in-memory deque of settled caption lines). The Bridge opens only `/tap`; the
Recorder owns all fan-out (ADR-0002).

Live captions are **session-scoped and ephemeral**: each line carries the
session snapshotted at tap open, the panel shows only the focused session's,
and a session's durable text is its merged transcript, not this feed.

## TapFanOut

The per-`/tap`-WebSocket lifecycle object that owns one
[Utterance](#utterance)'s audio fan-out — the concrete thing ADR-0002's "the
Recorder owns the fan-out internally" refers to. It owns the WAV file
(open / resume / finalize / unlink-when-empty), the `UtteranceIndex`
bookkeeping, the ActiveStream row, and the live leg — the last only by holding
one [TapRelay](#taprelay) and feeding each frame through it.

`do_record` / `do_live` are snapshotted at WS open, mirroring the global
recording toggle's next-utterance-not-this-one semantics.

## TapRelay

The per-`/tap` **live leg**: the sub-unit of [TapFanOut](#tapfanout) owning the
relay lifecycle, the per-tap [SpeechGate](#speechgate--gate_kind), and the
transparent reconnect-with-backoff across live-channel restarts. It is a
sub-unit, **not** a new architectural boundary — ADR-0002 is unchanged.

A relay that dies never stops the WAV: recording degrades gracefully,
independently of the live leg.

## Bridge

The platform-side audio tap that forwards remote-participant PCM to the
Recorder — a browser extension, a native tray app, or any other helper. Each
lives under `bridges/`.

Wire contract: one WebSocket per [Utterance](#utterance) to
`ws://<host>/tap?identity=…&name=…&tap_mode=…`, streaming raw 16 kHz mono int16
PCM frames (20 ms / 640 bytes per frame). That is the whole audio contract, and
it is held **structurally** — the
Recorder's constants are the source, `tools/stamp_tap_wire.py` writes every
restatement, and a drift fails CI (ADR-0019).

A Bridge may also issue a small **control** plane over HTTP under `/api/tap`,
authenticated by the tap token as a bearer. The surface is create / trigger /
poll / rotate / probe — never delete or prune — so a leaked tap token's blast
radius stays bounded. Artifacts ship on tagged releases (ADR-0012).

The mnemonic: **TapScribe** = Bridge (the Tap) + Recorder (the Scribe).

## Tray Bridge

The native tray-icon Bridge family: one cross-platform **bridge core** (the
meeting bracket, CaptureOrchestrator, Level gate, the `/tap` and control
clients) plus a thin per-OS **shell** contributing audio capture, device
enumeration, storage and the tray UI. Unqualified, it means the family;
qualify with the OS only when the shell matters. ADR-0020. There is ONE tray
per OS: in a [Bundle](#bundle) install it also carries the
[host role](#host-role). ADR-0022.
_Avoid_: "the tray app" (it is a Bridge — a [Bundle](#bundle) is what *is* a
Recorder), "the Mac bridge" for the family.

## Bundle

A self-contained, platform-native distribution of the Recorder: embedded
CPython, the wheel, and a tray, installed per-user with no Python prerequisite.
The tray points `TAPSCRIBE_BASE_DIR` at the operator's data directory, boots the
Recorder and supervises it — the [host role](#host-role); it is not the server,
it starts one. That tray is the [Tray Bridge](#tray-bridge), which is the same
executable the bridge-only artifact ships and carries the extra role because the
payload is beside it. Windows only; ADR-0015, ADR-0022. ADR-0024 (proposed) adds
the macOS shape.

A Bundle is **not a Bridge**: a Bridge taps audio *into* a Recorder, a Bundle
*is* one, packaged. Shipping a Bridge inside one is composition, not identity —
so a Bundle still never appears in the dashboard's "Get a bridge" card (you need
a Bundle to have a dashboard).
_Avoid_: "installer" (it names the act, leaving no word for the result), "the
exe", "the Windows app".

## Host role

The Tray Bridge's second role: boot, supervise and reap a
**co-located** Recorder, and be the way in to it (the
[login link](#login-link), the log, Start / Stop). Carried only when a host
payload sits beside the tray on disk — a [Bundle](#bundle) install — so the
role is a fact about the install rather than a setting. ADR-0022.

## HTTP auth gate · auth schemes

The single middleware (`auth.basic_auth_middleware`) that picks, for every HTTP
request, which of three schemes applies: **public** (an exact exempt route),
**tap-bearer** (anything under `/api/tap`, authenticated by the tap token), or
**Basic** (everything else, the dashboard). Exempt-from-Basic and
requires-bearer are ONE predicate, so a new `/api/tap` route is gated by
construction. ADR-0008.

The Basic scheme accepts two **credential forms** for the same secret: the
`Authorization: Basic` header, and a **dashboard session cookie** obtained by
spending a [login link](#login-link). Two forms, still one scheme — a route is
never gated differently depending on which the caller used. ADR-0023.

The `/tap` **WebSocket** is a fourth, separate path — middlewares don't see WS
upgrades, so it carries the token in `Sec-WebSocket-Protocol`.
_Avoid_: "tap auth" — say "the tap-bearer scheme" (HTTP) or "the `/tap`
subprotocol gate" (WS); they share a secret, never a transport.

## Login link

A single-use, short-lived URL that trades the dashboard password for a
[session cookie](#http-auth-gate--auth-schemes), so opening the dashboard is
one click instead of a password prompt. Minted only by a caller that can
already authenticate (the tray reads `.auth-password` off disk), spent once,
and never a second secret at rest.
_Avoid_: calling it a token or an API key — it authenticates a browser once,
and buys nothing a caller could not already do. ADR-0023.

## Bracketed meeting

A **Start meeting → End meeting** bracket a Bridge wraps around a recording so
the user gets meeting notes without opening the dashboard. It governs Session
**routing, not capture**: speakers are still tapped as they speak and a
[Mute](#mute) still ends an [Utterance](#utterance); the bracket only decides
*which* Session new taps feed.

Start mints a fresh [detached session](#detached-session) and routes every tap
to it. End closes every tap honouring [Drain](#drain), waits for a close-all
barrier so the last WAV is finalised, then triggers the [end-of-meeting
pipeline](#end-of-meeting-pipeline).

The detached Session id is the bracket's **durable handle**: persisted, it
survives End and a Recorder restart, so a meeting card re-derives progress and
the finished summary from it rather than caching either.

## Detached session

A session a Bridge creates for itself and directs its own taps into, isolated
from the Recorder's global current session, so two people can tap two meetings
against one Recorder without muddling. Minted via `POST /api/tap/new-session`
with `{"detached": true}` and joined with `?session=<id>`.

Affiliation is snapshotted at WS open, so a rotation never re-homes an open
tap. On disk it is an ordinary session — same layout, listing and maintenance,
**including empty-session pruning**, so a Bridge should mint one just-in-time
rather than long in advance.

## Current session · attached tap

The **current session** is the one session the Recorder is recording into right
now (`recorder.session_start`) — the session a `/tap` with no `?session=` is
routed to, and the one the dashboard's Sessions list badges `● live`. There is
exactly one, it is never a [detached session](#detached-session), and it moves
on rotation or restart.

An **attached tap** is a tap routed to it — as opposed to the
[bracketed meeting](#bracketed-meeting)'s detached one. A Bridge attaches by
simply omitting `?session=`, so the mode is a Bridge-side choice the Recorder
needs no opinion on. A Bridge is attached OR in a bracketed meeting, never both:
one device is one speaker, and one identity feeding two sessions at once would
split a speaker across them. ADR-0025.
_Avoid_: "the live session" in code and docs — it is the operator-facing
spelling only (the dashboard badge), and "live" elsewhere in the product means
the [LiveChannel](#livechannel--whisperlivekitchannel--moonshinelivechannel),
live captions and live taps, none of which are a session.

## Capture device · render device · loopback

The two kinds of audio endpoint a native Bridge can tap. A **capture device**
is an input (a microphone). A **render device** is an output (speakers) and is
the **loopback** candidate: capturing its mix records the system audio out —
the "other side" of a meeting, which has no mute event of its own.

Both sit behind the bridge core's one capture seam, so loopback flows through
the same resample → gate → `/tap` pipeline as a mic and the core never sees a
platform audio API.

## One device = one speaker · CaptureOrchestrator

The rule that a Bridge tapping several devices runs **one independent pipeline
per device**, each under its own stable identity, so recordings stay
attributable per source instead of mixed. **CaptureOrchestrator** is the bridge
core component owning that set: it starts one pipeline per selected device
(best-effort), rejects duplicate identities up front, and tears them down
together.

Duplicate rejection is load-bearing: the Recorder buckets WAVs by sanitised
identity, so a collision cross-attributes two devices into one speaker.

## Single-person tap · Multi-person tap

A property of each `/tap`: does this WS carry **one** human or **several**? The
canonical attribution axis, and what gates diarization — only a multi-person
tap is split into [Voices](#voice) (ADR-0021).
_Avoid_: "loopback" or "mic vs. system" for this distinction.

The Bridge declares it on the wire and the operator may override it durably per
identity; precedence is **operator override › bridge declaration › default
single**. A single-person tap can have its [candidate
languages](#candidate-languages--language-pin) narrowed to a pin; a
multi-person tap cannot, so each region resolves language on its own.

This **nuances** the [one-`/tap`-WS-=-one-speaker invariant](#invariants): what
a WS guarantees is one *attribution identity*, not literally one human.

## Voice

One speaker the diarizer distinguishes within one [multi-person
tap](#single-person-tap--multi-person-tap), in one session.
_Avoid_: cluster, speaker label, diarized speaker.

A Voice is **session-local** — Monday's `Speaker A` is not Tuesday's — so it is
never a [Person](#person--identity--roster--people-registry) registry key. The
operator maps it to a Person and that mapping is a run-stamped pointer; an
unmapped Voice binds to nobody. ADR-0021.

## Person · Identity · Roster · People Registry

The cross-session naming model (ADR-0009). Concepts that are easy to conflate:

- **Identity** — the bridge-stamped token per device/participant, stored
  untruncated. The cross-session join key. A WAV filename carries only a
  truncated slug of it, so the filename is **not** a reliable key; the full
  Identity comes from the Roster.
- **Occurrence** — one appearance of an Identity in one session. Its speaker
  key is the `identity`, and stays that way when the tap is diarized, since a
  [Voice](#voice) is session-local (ADR-0021, amending ADR-0009 §4).
- **Roster** — a per-session, machine-written sidecar mapping full identity to
  its name, source and WAVs. Separate from the operator-editable session meta.
- **Person** — a global entity: one display name plus a set of member
  Identities. Every new Identity auto-binds to its own Person, so the registry
  is never empty. **Merge** folds two together, **detach** pulls one Identity
  back out.
- **People Registry** — the one global store (`people.json`) and source of
  truth for names.
- **Override** — a per-session alias that beats the Person's global name for
  that session only.

Resolution precedence: **Override › Person name › bridge display name / slug**.

Two Bridge-side qualifiers for a shell tapping several devices: the **base
identity** is what it streams under when a device carries no Speaker ID of its
own; a device's **streaming identity** is its own Speaker ID, or the base when
that is blank.

## Utterance

A continuous speech segment from one speaker, delimited by [Mute](#mute)
boundaries on the Bridge side. One Utterance is one WAV on disk and — usually —
one `/tap` WebSocket; a mid-utterance blip reconnects under the same
[utterance_id](#utterance_id) and appends, so an Utterance is a logical concept
that can span several physical WSes.

## utterance_id

The UUID a Bridge generates per [Utterance](#utterance) and sends on `/tap`,
stable across reconnects so the Recorder resumes the same WAV. On-disk
filenames use a *separate* local UUID for collision-safety; don't conflate the
two.

## Mute

The Bridge-side signal that a speaker stopped talking — the only thing that
ends an [Utterance](#utterance) from the Bridge's side. If audio is still
buffered when it fires, the Bridge enters [Drain](#drain) rather than closing.

On platforms with no native mute event, the [Level gate](#level-gate)
synthesises it. Where a native mute *does* exist (a mic endpoint), it is an
authoritative **hard gate-closed** above the gate: a muted mic produces no taps
at all regardless of level, because it keeps delivering a residual the gate
alone would occasionally tap as a recurring "quiet" utterance.

## Level gate

The Bridge-side RMS **level** gate that decides [Utterance](#utterance)
boundaries where there is no native [Mute](#mute) — it is how the tray Bridge
produces one. Its knobs are the open threshold, the hangover (silence-to-close),
and the pre-roll (leading audio replayed on open).

Tuning is **per device, not global** (ADR-0007), so a system-loopback gate can
be more sensitive than a mic gate.
_Avoid_: borrowing [SpeechGate](#speechgate--gate_kind)'s "speech threshold"
vocabulary — the two gates sit on opposite sides of the `/tap` wire and never
share threshold units.

### Input-level meter

A per-device live RMS bar in the Bridge's Settings dialog, on the **same scale**
as the [Level gate](#level-gate) threshold and drawing that threshold as a
marker, so "is my voice above the line?" is answerable while tuning. Display
only — a second, throwaway capture that never touches the tap pipeline.
_Avoid_: conflating it with the Recorder-side post-gate level meter; opposite
side of the wire, and this one is a pre-gate tuning aid.

## Drain

The flush of trailing PCM buffered on the Bridge when [Mute](#mute) fired but
not yet delivered. The Bridge keeps its reconnect ladder running for up to
`DRAIN_MAX_MS` (8 s); once a `/tap` reconnects, the buffer is flushed before the
WS closes, so the WAV finalises with that audio intact. The budget exists so an
unreachable Recorder cannot wedge the Utterance forever.

## Blip-resilience recipe

The three numbers a Bridge uses to survive a transient `/tap` failure without
losing audio — the reconnect **backoff ladder**, the **gap-buffer** cap on PCM
held while disconnected, and the [Drain](#drain) budget. Named as one thing
because they only make sense together: a ladder without a buffer reconnects to
nothing, a buffer without a bounded drain wedges the Utterance.

Unlike the [wire contract](#bridge), these are **recommended, not enforced** —
the Recorder has no opinion on them and a third-party Bridge may deviate. What
is not allowed is the bundled Bridges and the docs drifting from each other.
**First-connect-failure semantics are pointedly not part of it**: the two
bundled Bridges answer that one differently on purpose.
_Avoid_: "reference recipe" — "reference implementation" already means the
local-test Bridge.

## Tail flush

The Recorder-side counterpart to [Drain](#drain), *not* a synonym. After a
`/tap` closes, the live relay still holds one in-flight caption line that never
got superseded; the tail flush emits it, so a short Utterance producing exactly
one caption doesn't vanish. The Bridge knows nothing about it.

## Invariants

Things the rest of the system relies on. Break one and something elsewhere
silently misbehaves; to relax one, document why and update this list.

- **One `/tap` WS = one speaker at a time.** Caption lines are attributed by
  the originating WS's identity. Nuanced, not broken, by a [multi-person
  tap](#single-person-tap--multi-person-tap): what a WS guarantees is one
  attribution identity.
- **One Utterance = one WAV.** A known `utterance_id` appends; a fresh one
  always means a fresh WAV. One WAV holding several Utterances must not happen.
- **Bridge → `/tap` is the only audio path.** Bridges never open a live-channel
  connection or POST settled lines back; the Recorder owns all fan-out.
- **Drain is bounded.** Unreachable Recorder ⇒ trailing audio is dropped rather
  than blocking the close forever. Don't remove the timeout.

## Interaction hold

The dashboard-wide rule that a per-tick render defers to operator interaction
state instead of destroying it. A host is held while a control inside it is
focused, a popover or `<dialog>` inside it is open, a text selection touches
it, or — for tail-following panels — the operator has scrolled away from the
tail.

A held render does not advance the render gate. It lands on the first tick
after the interaction clears, through the **tick-retry** — the one retry
mechanism in the app. Rule and rejected alternatives: ADR-0004; why there is
exactly one retry mechanism: ADR-0016. Call-site rules and the primitives:
CLAUDE.md.
_Avoid_: an ad-hoc description of a focus or selection guard. Say "this region
needs the interaction hold".

### Region · keyed list

The two shapes a held render takes. A **region** is swapped whole
(`renderRegion`). A **keyed list** has rows created once, matched by key, and
updated in place (`renderList`), never swapped — so its empty/loading
placeholder is a **sibling** of the host, toggled in place.

A keyed list owns two holds a region has no equivalent for. **The removal
hold**: a row holding a focused control whose key has left `items` is retained
at its position until a one-shot `focusout` — so a retained row is on screen
but not in `items`. **The per-row hold**: a focused control freezes its own
row's update and leaves that row's `itemSig` unstamped.

Which hold fires is a two-predicate question, and that is the point: removal
destroys focus on anything **focusable**, while an in-place write only threatens
**editable** state. So removal takes the wide predicate and the write holds take
the narrow one.
_Avoid_: "the list gate". Say "this is a keyed list" / "it needs the removal
hold".

## Pending edit

An inline edit the operator has typed but the server has not confirmed, held in
an **optimistic overlay** so the field — and every other region showing that
value — reads the typed text instead of snapping back on the next tick.
_Avoid_: bare "overlay" (the render canon's overlay is a popover/`<dialog>`).

A pending edit is saved by a **field saver** (one debounced PUT per id) and
cleared by the **catch-up sweep**, which drops every edit the server has caught
up to, once per tick, before anything computes a render signature — without it
a stale pending edit masks a later change made elsewhere forever. Deleting the
pending edit is also the only way to cancel its save.

A pending edit **pins its row into a filtered list** until the save settles.
This is not the mid-edit case — protecting the node being typed in is the
[interaction hold](#interaction-hold)'s job and keys on FOCUS. The pin keys on
the unsettled SAVE, covering the window after blur where the row would leave
and take with it the status cell a failure must land in.

Every save that reports itself narrates through the one **save-status
lifecycle**: `saving…`, then `saved` (auto-clearing) or `failed: <reason>`
(which stays until superseded). Each editable resource has ONE owner module
binding these together. Mechanism: CLAUDE.md.
_Avoid_: "set the status text". Say "wire it through the save-status
lifecycle".

## Lazy resource · resolve · failure policy · last-good hold

A **lazy resource** is one body the dashboard fetches on demand instead of
carrying on the `/api/state` poll, cached under a **signature key** that changes
exactly when the body does — the merged transcript, the persisted summary, the
per-session WAV listing, waveform peaks.
_Avoid_: "the transcript cache", "the files fetch" — they are the same thing
with different payloads, and treating them as different is what produced five
copies of the machinery (#222).

A consumer **watches** one once at build time, then **resolves** through that
watcher each tick. The resolve owns everything that would otherwise be
re-derived per call site: fetch the key once, apply the failure policy, and
answer `{ value, loading, stale, error }`. `loading` is true only on a genuine
**cold load** — the one state a caller may paint a placeholder for — and
`stale` says the value is the last-good body rather than this key's own.
_Avoid_: "the pending set", "the rerender-pending map" — that bookkeeping is the
resource's, not a view's. Don't say "pass a callback to resolve"; the callback
belongs to the watcher.

A resource's **failure policy** is declared once, at the resource, and is a
decision about the body: `retry-next-poll` (a rejection is silent and retries on
a later tick) or `remember-error` (report it and stop asking until the signature
changes — an unreadable WAV has no peaks, so re-asking every tick answers
nothing).

The **last-good hold** is stale-while-revalidate memory keyed by what the body
BELONGS to rather than which version it is. It is required when the signature is
a session-level aggregate that flips when any one sibling changes, since
otherwise every flip reports a cold load and the region blanks (#266). Omit it
when a stale body would be *wrong* rather than merely old.
_Avoid_: "the stale cache" — it holds exactly one value per session and exists
only to keep a region from blanking. Mechanism and the declaration rules:
CLAUDE.md.

## Player · seek target · open WAV · playhead

The dashboard's audio playback. There is exactly ONE **Player**, a single audio
element owned by the shell and mounted outside any view — a region, a
keyed-list row and a view host are all detached or swapped out from under a
playing element. Views own only **play affordances** that hand it a seek
target. It is driven by media events, not by the poll, so it sits outside the
render-signature and interaction-hold machinery entirely.
_Avoid_: audio widget, per-view player, transport.

A **seek target** is what an affordance hands the Player: the triple
(`source_wav`, source, `offset_s`) naming the exact file the audio is in and
where to land. A segment is never mapped onto a *different* file's timeline.
_Avoid_: "seeking to a time" — a time alone is not a target, since concurrent
per-speaker WAVs make session-relative time ambiguous.

**Every verb that removes audio must tell the Player.** This is a convention the
seam cannot enforce: each caller knows which files went and nothing downstream
can derive it. Skipping it fails silently and alarmingly — the browser has the
bytes buffered, so a deleted recording keeps playing to the end with no media
error at all.

The **playhead** is the Player's position drawn on the Recordings waveform,
shown only while the loaded file is exactly the file on the canvas. Clicking the
canvas is itself a seek target on it.
_Avoid_: cursor, position marker, scrubber (the scrubber is the Player's own
native control).

An **open WAV** is one a tap is writing right now. It is **not playable** — the
RIFF header is patched only when the tap closes, so the bytes declare a length
that isn't there yet.
_Avoid_: live WAV, current WAV (the *current session* is a different thing).

## Source pick · original / stripped · effective source

Which audio of a session the dashboard acts on: the **originals** as recorded,
or the **stripped** region clips a strip-silence run wrote. The pick belongs to
the SESSION, not to a view, so both stages read and write ONE store — picking
"stripped" in either is the pick the other lists, transcribes and plays from.
_Avoid_: "source toggle" (that is the CONTROL that writes the pick), per-view
source, audio mode.

The **effective source** is what every consumer must act on: it falls back to
"original" whenever the pick says "stripped" but the session has no `stripped/`
folder, so a stale pick can't operate on nothing.

Two conventions the seam cannot enforce, both failing silently. **A source
switch must drop the switching view's derived artifacts** — a strip preview
tuned against the original stays overlaid on the committed cut otherwise. And
**every verb that removes a session's stripped clips must clear the pick** —
`effectiveSource` masks a stale "stripped" only while the folder is gone, and
the session can regain one.

## Per-WAV transcript cache

The cached transcripts stored beside each transcribed WAV, one **directory per
WAV** holding one sidecar per `(backend, model)` plus a `_primary` pointer
naming the one the merge layer should use. `(backend, model)` is the cache key,
so transcribing with a different pair adds a sidecar rather than replacing one.

The authoritative `(backend, model)` for a transcript is the JSON inside, not
the filename — the filename is a stable index key. Layout, the legacy
single-sidecar migration and the public API live in `tapscribe/wav_cache.py`.

## Route module · State view

A **route module** is one file under `tapscribe/routes/` holding the routes of
one resource group, with a complete route map in its docstring. Modules group by
**domain concern rather than URL prefix**, so one may own routes spanning two
prefixes (ADR-0018). `routes/__init__.py` is the index; `app.py` holds no
routes.

Four support modules are the only thing a route module may import from a
sibling: `deps`, `body`, `errors` and `guards` (the destructive-route
preflight).

The **State view** (`state_view.py`) is the read model behind `GET /api/state`:
given one **`StateInputs`** — a frozen record of everything recorder-owned at
one instant — it produces the poll payload and its ETag. Request-free and
Recorder-free, so the projection is testable without a route. Distinct from the
[`sessions`](#session-modules--paths--listing--maintenance) read model, which
supplies the listing it joins in.

`StateInputs` is the whole interface — one value object, not a marshalled
argument list — so a tick can be built by hand and nothing the worker thread
touches is still being mutated.

## Session modules — paths · listing · maintenance

Recording-session bookkeeping is split across four modules by concern, so the
once-per-second read path isn't tangled with path-safety or destructive
operations:

- **`session_paths`** — the path-resolution seam: the ONE place a
  request-supplied `session` / `name` becomes a filesystem path under
  `RECORDINGS_DIR`, owning the two-layer `py/path-injection` guard. It is also
  the one owner of the on-disk session-layout filenames, which everything else
  composes onto an already-resolved dir instead of hand-typing. New code that
  turns request input into a recordings path goes through here.
- **`sessions`** — the dashboard read model: the poll-path listing (memoised on
  cheap stat signatures), session-meta read/write, and the lazy full-transcript
  reads the slim poll markers point at. It writes only its own sidecars —
  session meta and the summary — plus `repoint_voice_person`, the one verb that
  crosses sessions.
- **`session_maintenance`** — destructive, infrequent operator operations:
  absorb, delete audio / one WAV, prune, and the emptiness test. Resolves via
  `session_paths`, reads and writes meta via `sessions`.
- **`tap_registry`** — the canonical home of the in-flight tap mark
  (`mark_session_in_flight` / `release_session_mark` / `session_has_open_tap`)
  and of the destruction guard: a stdlib-only leaf importing nothing else in
  TapScribe, so both the recording hot path and the destructive-route preflight
  reach it without pulling maintenance weight. Not on the Recorder — the mark is
  not recorder state.

The recorder-filename parsers and builder live in `text`, the single source of
truth for the `<iso>_<speaker>_<ident>_<utt>.wav` format.

## Session job · `JobTracker.run`

The "one heavy job per session at a time" rule: a session may have at most one
transcribe **or** strip **or** summarize **or** end-of-meeting pipeline running.
Orchestrators bracket their work in the async context manager
`recorder.jobs.run(session, *, kind, …)`, which claims the slot on entry and
releases it on every exit path.

A busy session raises `SessionBusy` *before* the block runs, so a foreign claim
is never released — the guard is structural, not a try/finally discipline each
orchestrator re-derives.

## Batch transcription

The orchestrator driving a [Transcriber](#transcriber) across one WAV or a
session range, applying the per-session prompt/hotwords overrides, hallucination
filtering and the cache layer. Two entry points — one WAV, or every WAV in a
range — both claiming the [Session job](#session-job--jobtrackerrun) slot and
both running the meeting's **cover**: the generalist plus any specialist for the
meeting's [candidate languages](#candidate-languages--language-pin) (ADR-0011).

The module never raises `HTTPException`; it raises domain errors that one
registry maps to HTTP codes, so the same code can drive a CLI batch or a queue
worker. Its request value objects are the test surface.

## Batch strip

The strip-silence sibling of [Batch transcription](#batch-transcription) — same
orchestrator shape, same [Session job](#session-job--jobtrackerrun) claim, same
FastAPI-free contract. One entry point loops the per-WAV splitter over every
original WAV on a worker thread and aggregates.

Its request object owns the knob defaults; the route forwards only explicitly
provided values.

## Batch summarize

The post-transcription sibling of [Batch transcription](#batch-transcription) —
same orchestrator shape and contract. It reads the session's merged transcript,
builds a [Summarizer](#summarizer) via the factory *before* the slot claim so a
misconfigured source fails fast, runs it, and persists the result.

A per-session summarizer override beats the operator's persisted default, which
beats the bundled one.

## End-of-meeting pipeline

The strip → transcribe → summarize chain as **one** [Session
job](#session-job--jobtrackerrun): one trigger takes a finished session from raw
WAVs to a persisted summary with no operator in the loop. It owns no work loop —
each sibling exposes a `*_locked` core and the pipeline drives those under a
single claim, so the one-heavy-job rule holds across the whole chain.

Triggered and polled by a Bridge over two tap-bearer endpoints. **Operator
defaults only**: the trigger's body is ignored, never parsed, so a tap-token
holder can never choose what gets loaded or downloaded.

"Done" is answered from the persisted summary, so a Bridge polling across a
Recorder restart still gets it.

## Summarizer

The protocol for "something that can summarize one transcript":
`summarize(transcript, *, prompt) -> SummaryResult`. The post-transcription
mirror of [Transcriber](#transcriber), one altitude up, in
`tapscribe/summarizers/`. Three sources today: a **command** (pipes the
transcript to an operator-supplied CLI on stdin), a **local** bundled
hardware-routed model, and an **api** OpenAI-compatible endpoint.

`catalog` is the source-neutral model catalog AND the **security allowlist**: an
untrusted request-body model id only reaches a loader or a Hub download if it is
listed.

**Command preset** — a curated row a dashboard pick seeds into the Command
source's **editable** template field. Unlike the local-model catalog, the preset
list is **NOT an allowlist**: the template stays operator-trusted free text, and
what a preset adds is hardening-by-default. Don't confuse the two catalogs'
roles when adding rows to either.

## Wire-format note

Result JSON files name the engine under `"transcriber"`. Older recordings may
carry the key `"backend"` instead; readers accept both.
