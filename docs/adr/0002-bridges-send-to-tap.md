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

## Why one endpoint with internal fan-out

- **The Bridge contract collapses to "open WS, write PCM, close."**
  Every bridge platform (Chrome extension, Teams add-in, Python mic
  bridge) implements only capture → resample → frame → WebSocket — no
  JSON, no HTTP, no WlK protocol knowledge.
- **The Recorder owns every interaction with its supervised
  WhisperLiveKit child**, symmetric with how it owns every interaction
  with a `Transcriber` (ADR-0001) — runtime orchestration in one place,
  with one error-handling surface.
- **Per-speaker attribution is automatic.** Each `/tap` WS opens its
  own internal WlK relay, so that relay's settled lines are
  unambiguously that identity's speech.
- **Failure isolation and graceful degradation.** A WlK crash kills
  only that speaker's relay; WlK not running means the relay isn't
  attempted. Either way the WAV keeps being written, and the Bridge
  never has to decide anything about a missing live channel.

## Rejected alternatives

- **Bridge talks to WlK and the Recorder separately, POSTing settled
  lines back** (the prior shape): makes every bridge a WlK-protocol
  translator on top of an audio tap, sends the same bytes to the same
  machine twice, and routes settled lines WlK → Bridge → HTTP →
  Recorder when the Recorder is already the WlK child's parent.
- **Two WebSockets on the Recorder** (one for the WAV write, one for
  the WlK relay): same audio, same machine, two pipes — two connection
  lifecycles in every Bridge, and the pipes can drift out of sync
  within an utterance.

## Trade-offs accepted

- The Recorder is a WebSocket *client* as well as a server; the client
  is `tapscribe.live_relay.WlKRelay` (adds the small pure-Python
  `websockets` dependency).
- One WlK connection per active `/tap` WS — a 4-speaker meeting is 4
  concurrent localhost WlK connections. WlK handles that fine, but it's
  worth knowing when profiling connection counts.
- POST `/api/live-transcript` does not exist — settled lines never
  arrive over HTTP. The DELETE endpoint for the dashboard's "clear
  feed" button stays.
