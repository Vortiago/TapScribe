# Contributing to TapScribe

TapScribe is a small project; contributions of all sizes are welcome.

## Dev setup

```bash
git clone <your-fork-url> tapscribe
cd tapscribe
python -m venv .venv
source .venv/bin/activate          # or: .venv\Scripts\Activate.ps1 on Windows
pip install -e ".[dev]"
```

`[dev]` installs the runtime base plus the test/lint toolchain (pytest +
plugins, httpx, hypothesis, ruff, bandit, pip-audit, playwright, and the
audio-fixture helpers). To run the actual server you also need a model
backend — pick what matches your hardware:

```bash
pip install -e ".[whisper]"        # CPU / CUDA via faster-whisper + WhisperLiveKit
pip install -e ".[mlx]"            # Apple Silicon only
pip install -e ".[voxtral]"        # Mistral Voxtral via HF transformers
pip install -e ".[parakeet]"       # Parakeet TDT
pip install -e ".[moonshine]"      # Moonshine live captions
```

`onnxruntime` is a core dependency (it runs the vendored Silero VAD model
behind `tapscribe.vad`) and installs with a plain `pip install -e .`. The
`silero-vad` package and torch are NOT dependencies; #374 replaced them with
the vendored ONNX model.

## Running tests + lint

```bash
ruff check tapscribe tools tests benchmarks bridges/local-test-bridge
ruff format --check tapscribe tools tests benchmarks bridges/local-test-bridge
python -m pytest tests -q
```

All three run on CI for every push and PR — keep them green.

The Playwright dashboard-UI E2E (`tests/e2e/test_dashboard_ui.py`) needs
`python -m playwright install chromium` once; it self-skips when playwright
isn't importable.

The **headed bridge `browser_e2e`** tests load the real MV3 extension in a
*headed* Chromium and need a display — `pytest tests` skips them otherwise.
Run them under a virtual display:

```bash
xvfb-run -a python -m pytest \
  tests/e2e/test_bridge_extension_e2e.py \
  tests/e2e/test_bridge_meeting_e2e.py -m browser_e2e -q
```

The meeting flow also needs `pip install -e ".[whisper-cpu]"`. CI runs both
under xvfb in the `bridge E2E (extension + meeting)` job.

## Code style

- `ruff` is the only linter. Config is in `pyproject.toml`.
- Type hints are encouraged but not enforced.
- Tests for new pure helpers; the suite is deliberately scoped to things that
  don't require a model to load.

## Adding a model

Models are declared in the `TranscriberRegistry` in
`tapscribe/transcribers/catalog.py` (ADR-0003) — no hand-written dispatch;
`load_transcriber(model_name, *, backend)` resolves everything from it.

- Model in an existing family (e.g. another Whisper size): one new
  `ModelEntry` in `_DEFAULT_ENTRIES` — `model_id`, `family`, `display_name`,
  `description`, `languages`, `contexts` (batch/live picker gating),
  `backends`, `inputs`.
- New **family**: additionally write an adapter under
  `tapscribe/transcribers/` satisfying the `Transcriber` Protocol
  (`transcribers/base.py`), plus one `BackendBinding` per hardware kind —
  `kinds` (`{"cpu", "cuda"}`, `{"mlx"}`, …), a `loader(model_id, kind)`
  thunk, and a `probe_module` so `/api/models` can hide uninstalled families.

`resolve()` picks the first binding whose `kinds` contains the resolved
`BackendKind`; the factory caches per `(model_id, backend)`. Pin any
version-volatile upstream symbol with an `importorskip`-gated smoke test
(convention in `CLAUDE.md`).

## Reporting issues

Please include:

- Python version, OS, hardware (Apple Silicon vs Intel vs CUDA).
- TapScribe + WhisperLiveKit versions.
- The exact command and the first few seconds of console output — TapScribe
  logs the backend / device / model on every transcribe.
