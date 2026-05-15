# TapScribe

Local transcription recorder and operator dashboard.

TapScribe records one WAV per utterance per speaker over a WebSocket, runs
Whisper (or Voxtral) batch transcription on demand, and supervises a
[WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit) child process
for live captions. Nothing leaves the machine.

A FastAPI app serves a REST API and a dashboard at `/`:

- Start, stop, and restart `whisperlivekit-server` from the dashboard.
- One WAV per utterance per speaker, written to `recordings/<session>/`.
- Re-transcribe single WAVs or merge a whole session into one transcript.
- Hallucination filter with substring, `exact:`, and `re:` rules. Suppressed
  segments are kept in an audit array.
- Optional silero-VAD pass writes trimmed copies to `<session>/stripped/`.
  Originals are not touched.

Audio reaches TapScribe via a *bridge*: usually a browser extension that taps
the meeting platform's audio tracks and forwards raw PCM over WebSocket. The
included bridge, `spacialchat-bridge/`, targets spatial.chat. See
[`bridges/README.md`](bridges/README.md) for the wire protocol if you want to
add another.

## Dashboard

One operator console at `/`. Sessions list on the bottom-left, live
captions and active taps up top, merged transcript on the right after
the **▶ transcribe whole session** button runs.

![Merged session transcript](docs/dashboard-shots/06-real-audio-transcript.png)

The screenshot is captured live by the browser E2E test described
under [Tests](#tests), running the real Apollo 11 audio fixture
through the bridge and a real `faster-whisper` `tiny.en` over CPU.
Whisper self-flagged the imperfect output as low-confidence; bigger
models clean that up considerably.

## Quick start (macOS / Linux)

```bash
bash start.sh             # localhost only
bash start.sh --lan       # bind 0.0.0.0
```

The script finds Python 3.10+, creates `.venv`, installs dependencies
(`whisperlivekit`, `python-multipart`, `transformers`, plus `mlx-whisper` on
Apple Silicon), and launches TapScribe on port 8001 with
`whisperlivekit-server` as a child on port 8000. Child logs are prefixed
`[wlk]`. Ctrl+C stops both.

Open `http://localhost:8001/`. On first run two secrets are generated and
printed:

- A dashboard password for HTTP Basic auth, persisted to `.auth-password`.
- A `/tap` bearer token for the bridge, persisted to `.tap-token`. Paste it
  into the bridge popup along with the host and port.

Rotate with `--rotate-password` or `--rotate-tap-token`. Pass `--tls` to serve
`https://` and `wss://`; a self-signed cert is generated on first boot
(`.tapscribe-cert.pem`, `.tapscribe-key.pem`) and reused after. Supply your
own with `--cert <path> --key <path>`.

## Quick start (Windows / PowerShell)

```powershell
.\start.ps1
.\start.ps1 -Lan
```

## Configuration

Three text files under `config/` shape every job. All are re-read on every
job.

| File | Whisper feature | Format |
|---|---|---|
| `config/prompt.txt` | `initial_prompt` | Prose under ~150 words. Biases style and vocabulary. |
| `config/hotwords.txt` | `hotwords` | Comma- or space-separated proper nouns. Stronger than `initial_prompt` for names. faster-whisper only. |
| `config/hallucinations.txt` | Post-decode suppression | substring, `exact:`, or `re:`. Matches are kept in an audit array. |

Templates: `config/prompt.example.txt`, `config/hotwords.example.txt`.
`config/hallucinations.txt` ships with rules for common YouTube-trained
Whisper hallucinations.

## Architecture

One backend, one supervised child, N bridges. Audio flows in over WebSocket;
captions and recordings come out.

```mermaid
flowchart LR
    subgraph Meeting["Meeting platform (e.g. spatial.chat)"]
        Bridge["Bridge<br/>(browser extension<br/>or native helper)"]
    end

    subgraph Host["TapScribe host"]
        Backend["TapScribe backend<br/>FastAPI :8001<br/>/tap · /api · dashboard"]
        WLK["whisperlivekit-server<br/>:8000 (child process)"]
        WAVs[("recordings/<br/>&lt;session&gt;/*.wav")]
    end

    Operator["Operator browser<br/>(dashboard)"]

    Bridge -- "PCM 16k mono<br/>over WS /tap" --> Backend
    Backend -- "forwards PCM" --> WLK
    WLK -- "settled live captions" --> Backend
    Backend -- "one WAV per utterance" --> WAVs
    Operator <-- "HTTPS + dashboard WS" --> Backend
```

- **Bridges** tap a meeting platform's audio and stream raw PCM to `/tap`.
  One WebSocket per speaker per utterance. See [`bridges/README.md`](bridges/README.md).
- **Backend** (`tapscribe/`) fans each PCM frame out to two sinks: a
  per-utterance WAV on disk, and an internal relay to the supervised
  WhisperLiveKit child for live captions. It also serves the operator
  dashboard.
- **WhisperLiveKit** runs as a child process the backend starts, stops, and
  restarts from the dashboard. Bridges never talk to it directly.

### Audio pipeline (per utterance)

One `/tap` WebSocket = one utterance. Each PCM frame is tee'd: appended to
the per-utterance WAV on disk **and** forwarded to the WhisperLiveKit child
for live captions. Settled caption lines flow back to the operator
dashboard. WAV writing is independent of the live relay — if WhisperLiveKit
is down, recording still works.

```mermaid
sequenceDiagram
    autonumber
    participant B as Bridge
    participant T as /tap handler
    participant W as WAV file
    participant R as WlKRelay
    participant L as WhisperLiveKit
    participant D as Dashboard

    B->>T: open /tap?identity&name<br/>(one WS per utterance)
    T->>W: open recordings/<session>/<utt>.wav
    T->>R: connect to WhisperLiveKit

    loop each PCM frame (16 kHz mono s16le)
        B->>T: PCM bytes
        T->>W: append frame
        T->>R: forward frame
        R->>L: PCM
        L-->>R: settled caption line
        R-->>T: on_settled_line(text)
        T-->>D: push to live feed
    end

    B->>T: close (mute / leave)
    T->>W: finalise WAV<br/>(or delete if empty)
    T->>R: close
```

## Backends

| Model | Backend | Languages | Notes |
|---|---|---|---|
| `tiny.en` / `small.en` / `medium.en` | mlx-whisper (AS) / faster-whisper | English | `small.en` is the default. |
| `large-v3` | mlx-whisper (AS) / faster-whisper | Multilingual | MLX or CUDA; CPU is slow. |
| `nb-whisper-medium` / `nb-whisper-large` | faster-whisper on CT2 weights | Norwegian | Pulled from `NbAiLab/nb-whisper-*/ct2/`. No MLX. |
| `voxtral-mini` | HF transformers | EN/ES/FR/PT/HI/DE/NL/IT | First load downloads ~6 GB. Best on CUDA. |

On Apple Silicon, live and batch both route through mlx-whisper by default.
Pass `--no-mlx` to opt out.

## Tests

```bash
pip install -e ".[dev]"
python -m pytest -q
```

Three layers, all fast:

- **Unit + route tests** (`tests/test_*.py`) cover pure helpers
  (hallucination filter, prompt/hotwords reading, slug parsing, WAV I/O,
  model routing) and FastAPI routes via `TestClient`. Whisper / Voxtral
  backends are stubbed; the suite stays under 20 s.
- **HTTP pipeline E2E** (`tests/e2e/test_pipeline_e2e.py`) boots a real
  uvicorn server, streams two synthetic WAVs concurrently through real
  `/tap` WebSockets, then walks every dashboard HTTP route to verify
  the recorder finalised the WAVs, fanned settled lines into the live
  feed, and produced a merged session transcript. Uses a
  `FakeTranscriber` so the test runs without faster-whisper installed.
- **Real-Whisper E2E** (same file, `test_pipeline_with_real_whisper`)
  streams committed CC-licensed audio fixtures (Apollo 11 English, Marlene
  Dietrich Norwegian) through the bridge and runs real `faster-whisper`
  on what the recorder wrote. Skipped automatically when
  `faster-whisper` isn't installed. See
  [`tests/fixtures/audio/README.md`](tests/fixtures/audio/README.md) for
  licence details and how to add more clips.
- **Dashboard UI E2E** (`tests/e2e/test_dashboard_ui.py`) launches
  headless Chromium via Playwright against the running server and
  asserts on actual DOM. Two variants:
  - The fast plumbing check (synthetic WAVs + `FakeTranscriber`)
    verifies active-taps rows appear while bridges stream, settled
    lines land in the live transcripts panel with correct per-speaker
    attribution, and the **▶ transcribe whole session** button
    renders the merged transcript with both speakers' text.
  - The real-audio check (`@pytest.mark.real_audio`) streams the
    committed Apollo 11 fixture through the bridge, clicks the same
    button, and waits for real `faster-whisper` to produce a merged
    transcript in the UI. This is what produces the screenshot in
    the [Dashboard](#dashboard) section above.

  ```bash
  pip install -e ".[dev]" && python -m playwright install chromium
  python -m pytest tests/e2e/test_dashboard_ui.py
  ```

GitHub Actions runs the suite and `ruff check` on every push and PR across
Python 3.10-3.13 on Ubuntu, macOS, and Windows.

## License

MIT. See [LICENSE](LICENSE).
