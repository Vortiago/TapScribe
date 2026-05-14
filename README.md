# TapScribe

Local-first transcription recorder + operator dashboard.

TapScribe captures one WAV per utterance per speaker over a WebSocket, runs
Whisper (or Voxtral) batch transcription on demand, and supervises a long-
running [WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit) child
process for live captioning. Everything runs locally; no audio leaves the
machine.

A FastAPI app exposes both a REST API and an operator dashboard at `/`:

- **Live channel** — start / stop / restart `whisperlivekit-server` from
  the dashboard, change models without touching the terminal.
- **Per-utterance WAVs** — one connection per speaker per utterance, saved
  under `recordings/<session>/`.
- **Batch transcription** — re-transcribe single WAVs or merge a whole
  session into a chronological transcript. Quality knobs preset for
  meeting audio.
- **Anti-hallucination filter** — three-mode rules file (substring / exact
  / regex) drops YouTube-subtitle phrases Whisper learned to emit on
  silent stretches, with full audit trail.
- **Silence stripping** — optional silero-VAD pass writes trimmed copies
  to `<session>/stripped/`, originals untouched. Runs from the dashboard
  or from `python tools/strip_silence_cli.py`.

Audio reaches TapScribe via a **bridge** — typically a browser extension
that taps the meeting platform's remote audio tracks and forwards them
as raw PCM over WebSocket. The first such bridge, `spacialchat-bridge`,
targets spatial.chat and lives under `bridges/spacialchat-bridge/` in
this repo. Additional bridges for other platforms (Teams, Meet, Zoom,
etc.) can be added alongside it — see
[`bridges/README.md`](bridges/README.md) for the wire protocol.

## Quick start (macOS / Linux)

```bash
bash start.sh             # localhost only
bash start.sh --lan       # bind to 0.0.0.0 so other machines can connect
```

The script will:
1. Find a Python 3.10+ (`brew install python@3.13` on macOS).
2. Create a venv at `.venv` if missing.
3. Install `whisperlivekit`, `python-multipart`, `transformers`, and on
   Apple Silicon also `mlx-whisper`. The first install pulls PyTorch —
   several hundred MB.
4. Launch TapScribe (port 8001) and let it spawn `whisperlivekit-server`
   (port 8000) as a child. Logs from the child are prefixed `[wlk]`.
5. Ctrl+C stops both cleanly.

Open `http://localhost:8001/` in a browser. A dashboard password is
generated on first run and printed to the terminal (persisted to
`.auth-password`).

## Quick start (Windows / PowerShell)

```powershell
.\start.ps1
.\start.ps1 -Lan
```

## Configuration

Three small text files under `config/` shape every transcription job. All
of them are re-read on every job — edit, save, click, done.

| File | Whisper feature | Format |
|---|---|---|
| `config/prompt.txt` | `initial_prompt` | Flowing prose under ~150 words. Biases style + vocabulary. |
| `config/hotwords.txt` | `hotwords` | Comma- or space-separated proper nouns. Stronger than `initial_prompt` for names. faster-whisper only. |
| `config/hallucinations.txt` | Post-decode segment suppression | Three modes — substring / `exact:` / `re:`. Suppressed segments are kept in an audit array. |

See `config/prompt.example.txt` and `config/hotwords.example.txt` for
templates. `config/hallucinations.txt` ships with a starter ruleset for
the common YouTube-trained Whisper hallucinations.

## Project layout

```
tapscribe/                  Python package — the backend
├── __main__.py             CLI entry: `python -m tapscribe`
├── config.py               Paths, env, feature flags
├── text.py                 Pure helpers — prompt/hotwords, slug parsing
├── hallucinations.py       Filter parser + matcher
├── audio.py                WAV duration / RMS / PCM decoding
├── strip_silence.py        Silence detector (silero + RMS fallback)
├── models.py               Backend routing (faster-whisper / mlx / Voxtral / NB-Whisper)
├── transcribe.py           Per-WAV transcription functions
├── sessions.py             Folder layout + metadata + strip-silence
├── live.py                 WhisperLiveKit child-process management
├── auth.py                 HTTP Basic auth
├── app.py                  FastAPI app + routes
└── web/                    Dashboard HTML / CSS / JS modules

bridges/                    Platform bridges (one subdir per platform)
└── spacialchat-bridge/     Chrome MV3 extension for spatial.chat

config/                     User-editable prompt / hotwords / hallucinations
tools/                      Standalone CLIs (bench, strip-silence)
tests/                      pytest suite for pure helpers
```

## Backends

| Model | Backend | Languages | Notes |
|---|---|---|---|
| `tiny.en` / `small.en` / `medium.en` | mlx-whisper (AS) / faster-whisper | English | `small.en` is the default sweet spot. |
| `large-v3` | mlx-whisper (AS) / faster-whisper | Multilingual | Use on MLX or CUDA; CPU is slow. |
| `nb-whisper-medium` / `nb-whisper-large` | faster-whisper on CT2 weights | Norwegian-tuned | Pulled from `NbAiLab/nb-whisper-*/ct2/`. No MLX yet. |
| `voxtral-mini` | HF transformers | EN/ES/FR/PT/HI/DE/NL/IT | First load downloads ~6 GB. Best on CUDA. |

Apple Silicon (M1/M2/M3/M4) auto-routes both live AND batch through
mlx-whisper for ~3–5× speedup over CPU faster-whisper. Pass `--no-mlx`
to opt out.

## Tests + CI

```bash
pip install pytest fastapi numpy
python -m pytest tests -q
```

The test suite covers the pure helpers — hallucination filter, prompt/
hotwords reading, slug parsing, WAV I/O, model routing. It deliberately
does not exercise the actual Whisper / Voxtral / silero backends so it
stays fast and CI-friendly.

GitHub Actions runs the suite + `ruff check` on every push and pull
request, across Python 3.10–3.13 on Ubuntu, macOS, and Windows.

## License

MIT — see [LICENSE](LICENSE).
