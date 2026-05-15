---
status: accepted
date: 2026-05-15
---

# Bridges send to `/tap`; the Recorder fans out internally

The Bridge contract is one WebSocket per utterance to a single endpoint
(`/tap`), streaming raw PCM. The Recorder writes those bytes to a WAV
*and* relays them to its supervised WhisperLiveKit child for live
captioning, then consumes the resulting settled lines into
`LiveTranscripts` itself. Bridges never talk to WhisperLiveKit and
never POST settled lines back.

## Considered alternatives

**Bridge talks to two endpoints + posts settled lines back** (the
pre-refactor shape). The Bridge would open one WebSocket to
WhisperLiveKit (`ws://host:8000/asr`), one WebSocket to the Recorder
(`/record`), AND HTTP-POST settled lines from WlK's responses to
`/api/live-transcript` so the dashboard could see them.

Rejected because:
- The Bridge doubles as a protocol translator between WlK's JSON output
  and the Recorder's HTTP ingest — that role has nothing to do with
  tapping audio. Mixing them forces every Bridge author to re-implement
  the same translator.
- The same audio crosses the wire twice (once to WlK, once to
  `/record`), one for captioning and one for recording. They're the
  same bytes going to the same machine.
- Settled lines take a pointless round-trip: WlK → Bridge → HTTP →
  Recorder. The Recorder is already the WlK child's parent process; it
  has direct access.
- Each new bridge platform (Chrome, Teams, Zoom) re-implements the
  WlK protocol parsing and the HTTP POST plumbing, instead of just
  audio capture.

**Bridge sends to two separate WebSockets on the Recorder.** The Bridge
opens `/record` for the WAV write and `/live-audio` for the WlK relay
(both terminating at the Recorder; Recorder relays one of them to WlK).

Rejected because:
- Same audio, same machine, two pipes. Wasted bandwidth.
- Two connection lifecycles to manage in every Bridge.
- Splitting an utterance's audio across two WSes risks the two pipes
  going out of sync (one starts streaming before the other; one
  finishes after the other).

## Why one endpoint with internal fan-out wins

- **Bridge contract collapses to "one WS per utterance, send PCM."** A
  Chrome extension, a Teams add-in, a Python mic test bridge — they all
  implement the same minimum: open WS, write bytes, close.
- **The Recorder owns every interaction with its supervised
  WhisperLiveKit child.** That's symmetric with how it owns every
  interaction with a `Transcriber` (per ADR-0001) — runtime
  orchestration lives in one place, with one error-handling surface.
- **Per-speaker attribution is automatic.** Each `/tap` WS opens its
  own internal WlK relay, so settled lines emitted by that relay are
  unambiguously the speech of that WS's identity.
- **Failure isolation.** If WlK crashes for one speaker, only that
  relay dies. WAV recording continues regardless. Other speakers'
  relays stay healthy.
- **Graceful degradation.** WlK not running → relay isn't attempted →
  WAV still written → operator sees the live-channel state on the
  dashboard. The Bridge never has to decide what to do about a missing
  live channel.

## Trade-offs accepted

- **The Recorder is now a WebSocket *client*, not just a server.** Adds
  `websockets` (a tiny pure-Python library) as a base dependency. The
  client lives in `tapscribe.live_relay.WlKRelay`.
- **One WlK connection per active `/tap` WS.** A 4-speaker meeting =
  4 concurrent WlK connections from localhost. WhisperLiveKit handles
  this fine (it's the same N-clients pattern the old Bridge produced),
  but it's worth knowing if someone profiles connection counts.
- **POST `/api/live-transcript` is removed.** Anyone who had been using
  it (no committed callers in this repo; the spacialchat-bridge isn't
  uploaded yet) will get 405 Method Not Allowed. The DELETE endpoint
  for the dashboard's "clear feed" button stays.

## Consequences

- New bridges only have to implement: capture → resample → frame →
  WebSocket. No JSON, no HTTP, no WlK protocol knowledge.
- `bridges/local-test-bridge/` becomes trivial — see Land 5b.
- The `spacialchat-bridge` source (still pending upload) needs to
  follow the new contract; the speculative wire-protocol description
  in `bridges/spacialchat-bridge/README.md` was updated to match.
- The `LiveChannel` / `ActiveStreams` / `LiveTranscripts` triple in
  `CONTEXT.md` reframed: from "three independent dataflows the Bridge
  produces" to "three internal Recorder concerns the `/tap` endpoint
  fans into."
