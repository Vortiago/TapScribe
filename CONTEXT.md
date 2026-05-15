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
`transcribe(path, prompt, hotwords) -> TranscriptionResult`.

Concrete implementations:
- `FasterWhisperTranscriber` — faster-whisper / CTranslate2 on CPU. Also
  serves NB-Whisper checkpoints (the `nb-whisper-*` family loads via the
  same backend on its `ct2/` weights).
- `MlxWhisperTranscriber` — mlx-whisper on Apple Silicon GPU.
- `VoxtralTranscriber` — Mistral Voxtral via HuggingFace transformers.

Each Transcriber declares its `device` and `name` (`"faster-whisper"`,
`"mlx-whisper"`, `"voxtral"`); these strings appear in result JSON under
the `"transcriber"` key and on the dashboard.

Note: there is also a **live transcriber** — the `whisperlivekit-server`
child process the Recorder supervises for streaming captions. It is not
a `Transcriber` in the protocol sense (no `transcribe(path, …)` call);
when precision matters, say "the live channel" or "the WhisperLiveKit
child" rather than "the live Transcriber."

## TranscriptionResult

The frozen-dataclass return value of `Transcriber.transcribe(...)`. Carries
the segments, the joined plain text, the metadata about which transcriber
+ model + device produced it, and the inputs that were in effect
(`initial_prompt_used`, `hotwords_used`, `quality_settings`).

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

## Wire-format note

Result JSON files (per-WAV `<name>.json`, `session-transcript.json`)
use `"transcriber": "faster-whisper" | "mlx-whisper" | "voxtral"`. This
is a rename from a prior `"backend"` field; older recordings written
before the rename may still use the old key.
