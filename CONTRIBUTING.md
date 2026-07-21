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

`onnxruntime` is a core dependency (it runs the vendored Silero VAD model
behind `tapscribe.vad`, used by both the live SpeechGate and the
strip-silence detector) so it installs automatically with
`pip install -e .` — no extra needed. The `silero-vad` package and torch
are NOT dependencies; #374 replaced them with the vendored ONNX model.

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

The meeting flow also needs `pip install -e ".[whisper-cpu]"` for the real
transcribe. CI runs both under xvfb in the `bridge E2E (extension + meeting)`
job.

## Code style

- `ruff` is the only linter. Config is in `pyproject.toml`.
- Type hints are encouraged but not enforced.
- Tests for new pure helpers; the test suite is deliberately scoped to
  things that don't require a model to load.

## Adding a model

Models are declared in the `TranscriberRegistry` — the single source of
truth in `tapscribe/transcribers/catalog.py` (ADR-0003). There is no
hand-written dispatch to touch: the factory `load_transcriber(model_name,
*, backend)` resolves everything from the registry.

Adding a model that fits an existing family (e.g. another Whisper size)
is **one new `ModelEntry`** in `_DEFAULT_ENTRIES`:

```python
ModelEntry(
    model_id="voxtral-mini",          # canonical short name (the API/config value)
    family="voxtral",                 # drives the dashboard <optgroup>
    display_name="voxtral-mini",
    description="Mistral Voxtral 3B · 8 langs · no Norwegian",
    languages=("en", "es", "fr", ...),  # ISO codes, or ("auto",) for auto-detect
    contexts=_BATCH_ONLY,             # "batch" / "live" — gates which picker shows it
    backends=_VOXTRAL_BACKENDS,       # a shared bindings tuple (below)
    inputs=NO_INPUTS,                 # per-model form fields the dashboard renders
),
```

Adding a whole new **family** additionally needs an adapter and its
bindings, both reusable across every model in the family:

1. Write the adapter loader under `tapscribe/transcribers/` — a class
   satisfying the `Transcriber` Protocol (`tapscribe/transcribers/base.py`):
   `name`, `device`, `model_name`, `backend`, and
   `transcribe(path, *, initial_prompt, hotwords, source_lang)
   -> TranscriptionResult`.
2. Add one `BackendBinding` per hardware kind it serves — each pairs a
   `kinds` set (`{"cpu", "cuda"}`, `{"mlx"}`, …) with a `loader(model_id,
   kind)` thunk and a `probe_module` (the top-level import that signals the
   dependency is installed, so `/api/models` can hide unavailable families):

   ```python
   _PARAKEET_BACKENDS = (
       BackendBinding(kinds=frozenset({"mlx"}), loader=_load_parakeet_mlx, probe_module="parakeet_mlx"),
       BackendBinding(kinds=frozenset({"cuda", "cpu"}), loader=_load_parakeet_hf, probe_module="transformers"),
   )
   ```

`resolve()` walks an entry's `backends` in order and picks the first
binding whose `kinds` contains the resolved `BackendKind`; the factory
caches per `(model_id, backend)`. See `docs/adr/0003-transcriber-registry.md`
for the rationale, and pin any version-volatile upstream symbol with an
`importorskip`-gated smoke test (see the convention in `CLAUDE.md`).

## Reporting issues

Please include:
- Python version, OS, hardware (especially Apple Silicon vs Intel vs CUDA).
- The TapScribe + WhisperLiveKit versions you have installed.
- The exact command you ran and the first few seconds of console output —
  TapScribe logs the backend / device / model on every transcribe.
