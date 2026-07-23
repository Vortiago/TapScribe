"""TapScribe — a local-first transcription recorder + dashboard.

TapScribe captures one WAV per utterance per speaker over a WebSocket,
runs Whisper batch transcription on demand, and manages a long-running
WhisperLiveKit child process for live captioning. A single FastAPI app
exposes both a REST API and an operator dashboard at /.

The package is split into focused modules:

  config         Module-level constants, paths, env, and feature detection.
  text           Pure string/text helpers (prompt+hotwords reading, slug parsing).
  hallucinations Parser + matcher for the hallucination filter file.
  audio          WAV I/O and PCM helpers.
  transcribers   Stateful Transcriber adapters + load_transcriber factory.
  nb_whisper     NB-Whisper CT2 weight download (used by FW batch + live).
  wav_cache      Per-WAV transcript sidecar read/write.
  sessions       Recording-session bookkeeping (folder layout, meta, strip-silence).
  session_merge  Pure selection + merge of per-WAV results into a session.
  live           WhisperLiveKit child-process management.
  live_relay     Recorder-side WebSocket client to the live child.
  tap_fan_out    Per-/tap-WS lifecycle: WAV write + WlK relay fan-out.
  recorder       The Recorder context object + composed sub-components.
  auth           Basic-auth middleware + /tap subprotocol gate.
  app            The FastAPI app object and its routes.
"""

__all__ = ["__version__"]
__version__ = "1.2.0"
