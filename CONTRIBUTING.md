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
pip install -e ".[vad]"            # silero-vad for the strip-silence path
```

## Running tests + lint

```bash
ruff check tapscribe tools tests
python -m pytest tests -q
```

Both run on CI for every push and PR — keep them green.

## Code style

- `ruff` is the only linter. Config is in `pyproject.toml`.
- Type hints are encouraged but not enforced.
- Tests for new pure helpers; the test suite is deliberately scoped to
  things that don't require a model to load.

## Adding a model backend

The hot-loop is `tapscribe.models.get_model` and the per-WAV functions in
`tapscribe.transcribe`. New backends get a tagged tuple in the model
cache and a dispatch branch in `transcribe_wav_sync`. Mirror the shape
of the existing result dicts so the dashboard renders consistently.

## Reporting issues

Please include:
- Python version, OS, hardware (especially Apple Silicon vs Intel vs CUDA).
- The TapScribe + WhisperLiveKit versions you have installed.
- The exact command you ran and the first few seconds of console output —
  TapScribe logs the backend / device / model on every transcribe.
