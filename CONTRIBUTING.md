# Contributing to TapScribe

Thanks for the interest! TapScribe is a small project; contributions of
all sizes are welcome.

## Dev setup

```bash
git clone <your-fork-url> tapscribe
cd tapscribe
python -m venv .venv
source .venv/bin/activate          # or: .venv\Scripts\Activate.ps1 on Windows
pip install -e ".[dev]"
```

That installs the runtime base + `pytest`, `pytest-cov`, and `ruff`.

To run the actual server you'll also want one or more of the model
backends — pick what matches your hardware:

```bash
pip install -e ".[whisper]"        # CPU / CUDA via faster-whisper + WhisperLiveKit
pip install -e ".[mlx]"            # Apple Silicon only
pip install -e ".[voxtral]"        # Mistral Voxtral via HF transformers
```

silero-vad + torch are core dependencies (used by both the live
SpeechGate and the strip-silence detector) so they install
automatically with `pip install -e .` — no extra needed.

## Running tests + lint

```bash
ruff check tapscribe tools tests
python -m pytest tests -q
```

Both run on CI for every push and PR — keep them green.

The **headed bridge `browser_e2e`** tests load the real MV3 extension in a
*headed* Chromium, which needs a display — `python -m pytest tests -q` skips
them unless one is present (you'll see a `needs a display — run under xvfb`
skip). Run them under a virtual display:

```bash
xvfb-run -a python -m pytest \
  tests/e2e/test_bridge_extension_e2e.py \
  tests/e2e/test_bridge_meeting_e2e.py -m browser_e2e -q
```

The meeting flow also needs `pip install -e ".[whisper-cpu,vad]"` for the real
transcribe. CI runs both under xvfb in the `bridge E2E (extension + meeting)`
job.

## Code style

- `ruff` is the only linter. Config is in `pyproject.toml`.
- Type hints are encouraged but not enforced.
- Tests for new pure helpers; the test suite is deliberately scoped to
  things that don't require a model to load.

## Adding a model backend

Add a new adapter module under `tapscribe/transcribers/` exposing a class
that satisfies the `Transcriber` Protocol (see
`tapscribe/transcribers/base.py`): `name`, `device`, `model_name`, and
`transcribe(path, *, initial_prompt, hotwords) -> TranscriptionResult`.
Wire a dispatch branch into `_build_transcriber` in
`tapscribe/transcribers/__init__.py`. The factory caches per
`(model_name, use_mlx)` automatically.

## Reporting issues

Please include:
- Python version, OS, hardware (especially Apple Silicon vs Intel vs CUDA).
- The TapScribe + WhisperLiveKit versions you have installed.
- The exact command you ran and the first few seconds of console output —
  TapScribe logs the backend / device / model on every transcribe.
