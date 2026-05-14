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
  models         Backend routing (faster-whisper / mlx-whisper / Voxtral / NB-Whisper).
  transcribe     Per-WAV synchronous transcription functions.
  sessions       Recording-session bookkeeping (folder layout, meta, strip-silence).
  live           WhisperLiveKit child-process management.
  auth           Basic-auth password handling + middleware.
  app            The FastAPI app object and its routes.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
