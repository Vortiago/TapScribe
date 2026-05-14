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

Three independent dataflows that all involve the Bridge but play different
roles. Easy to confuse from names alone; the dashboard's three panels each
map to exactly one of these.

| Concept | What it owns | Inbound from the Bridge | Dashboard panel |
|---|---|---|---|
| **LiveChannel** | The supervised `whisperlivekit-server` child process (port 8000). | WebSocket carrying raw PCM frames for live captioning. | "live channel" |
| **ActiveStreams** | The map of currently-open `/record` WebSockets that are writing per-utterance WAVs (port 8001). | One WebSocket per remote participant per utterance, raw PCM frames. | "active streams" |
| **LiveTranscripts** | A bounded in-memory deque of settled caption lines (max 200). | HTTP POSTs to `/api/live-transcript` carrying the Bridge's view of WhisperLiveKit's settled output. | "live transcripts" |

The Bridge is the producer for all three; the Recorder is the consumer.
None of the three sees the others — settled lines from the LiveChannel
don't automatically populate LiveTranscripts (the Bridge does that
explicitly), and a `/record` WebSocket carries audio that's totally
independent of the LiveChannel's audio stream.

## Bridge

The platform-side audio tap that forwards remote-participant PCM to the
Recorder. Typically a browser extension (Chrome MV3 today) but can be
any native helper for a different platform — Teams add-in, Zoom plugin,
etc. Each Bridge talks the same wire protocol (see
`bridges/README.md`). Bridges live in `bridges/<platform>-bridge/`.

## Wire-format note

Result JSON files (per-WAV `<name>.json`, `session-transcript.json`)
use `"transcriber": "faster-whisper" | "mlx-whisper" | "voxtral"`. This
is a rename from a prior `"backend"` field; older recordings written
before the rename may still use the old key.
