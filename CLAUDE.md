# Pointers for Claude

- `CONTEXT.md` — domain glossary (Recorder, Bridge, Utterance, Drain, …).
  Use those names; don't introduce shadow vocabulary.
- `.claude/hooks/` — `session-start.sh` installs deps, `stop.sh` runs ruff.
  Convention changes go there or in `pyproject.toml`, not here.
- `bridges/README.md` — Bridge → `/tap` wire contract.

## Runtime deps the install picker does NOT cover

`tools/install_picker.py` resolves *model* extras (`whisper-cpu`,
`parakeet-mlx`, …). One recurring runtime dependency falls outside
that matrix and is wired into `start.sh` / `start.ps1` instead, after
the picker runs:

- **`silero-vad` (`[vad]` extra, pulls `torch>=2.1`)** — the per-tap
  TapScribe gate (`gate_kind="tapscribe"`, the default) imports
  `silero_vad` lazily on the first `/tap` WS. Missing → the tap logs
  `gate construction failed … falling back to passthrough` and the
  gate the operator picked is silently a no-op. `start.sh` runs
  `pip install -e ".[vad]"` when the module isn't importable.

**`ffmpeg` is NOT required, period.** Every MLX backend
(`mlx_whisper`, `mlx_parakeet`, `mlx_canary`) pre-decodes the
recorder's WAV via `tapscribe.wav_predecode.load_recorder_wav_as_pcm`
into a numpy float32 array and hands it directly to the model's
array-accepting entry point:

- `mlx_whisper.transcribe(array, …)` for Whisper. The model's own
  30-s internal windowing covers long inputs.
- `parakeet_mlx.audio.get_logmel(mx.array(array), preproc)` →
  `model.generate(mel)` for Parakeet, **inside a chunking loop**
  (`chunk_duration_s` / `overlap_duration_s` knobs on
  `MlxParakeetTranscriber`) so long sessions stay under the Metal
  GPU's per-buffer cap. Per-window timestamps are shifted by the
  window's start so the merged result is session-relative.
- `mlx_audio.stt.models.canary.Model.generate(array, …)` for Canary,
  **also chunked** — the upstream `max_tokens=200` default caps a
  single call to roughly 30 s of speech, so the adapter calls
  generate once per ~30 s window and stitches text. Canary 0.4.x's
  `STTOutput` no longer reports segment or word-level timestamps,
  so the adapter synthesises segment start/end from the window
  offsets.

The pre-decode trick lives in its own module
(`tapscribe/wav_predecode.py`) so the next contributor poking at
"where do we skip ffmpeg" finds it on the first grep. The chunking
loops live next to their adapter and share the same shape.

There is **no path-based fallback**. Non-recorder WAVs (different
sample rate / channel layout / sample width) raise a clear
`RuntimeError("unexpected WAV format …")` at pre-decode time — the
operator's signal to convert the file, not a cue to silently
re-introduce ffmpeg. Reintroducing a `model.transcribe(str(path))`
fallback would defeat the whole point and is rejected at review.

The chunk-size knobs are env-tunable today
(`TAPSCRIBE_PARAKEET_CHUNK_S`, `TAPSCRIBE_PARAKEET_OVERLAP_S`,
`TAPSCRIBE_CANARY_CHUNK_S`, `TAPSCRIBE_CANARY_OVERLAP_S`,
`TAPSCRIBE_CANARY_MAX_TOKENS`). A follow-up PR will plumb per-request
values from the dashboard through `BatchSessionRequest` so operators
can tune from the UI without restarting the recorder — every
operator-tunable setting belongs in the dashboard, see the
strip-silence knobs in `web/components/session-detail.html` for the
pattern.

If a new runtime dep with the same shape (system binary, or optional
Python package gated by a lazy import) lands, add it to the `Runtime
python deps` block in `start.sh` rather than as a Python preflight —
operators hit it once on bring-up instead of mid-request.

### Upstream MLX symbols: lock the contract with a smoke test

The MLX adapters import undocumented symbols from `mlx_whisper`,
`parakeet_mlx.audio`, and `mlx_audio.stt.models.canary`. Upstream
renames (Canary was renamed `Canary` → `Model` between mlx-audio
0.3.x and 0.4.x) silently break those imports at request time,
*after* a clean unit-test run that mocked the model object. The
convention to catch this earlier:

- **Pin a narrow upper bound** in `pyproject.toml`
  (`mlx-audio>=0.4,<0.5`, `parakeet-mlx>=0.5,<0.6`, etc.) — primary
  defence.
- **Add an `importorskip`-gated smoke test** that asserts every
  imported symbol exists with the expected shape (signature kwargs,
  classmethod presence). One per adapter, at the bottom of
  `tests/test_transcribers_mlx_*.py`. The test no-ops on hosts
  where the upstream package isn't importable (the Linux CI matrix)
  and runs in full on the macOS-arm64 CI runner.

A future upstream API change then fails CI on the PR that bumps the
pin instead of failing in production weeks later. The pattern is
short — see `test_mlx_audio_canary_upstream_contract` in
`tests/test_transcribers_mlx_canary.py` for the template.

## Security: avoid CodeQL "security-and-quality" tripwires

PRs are scanned by CodeQL with the `security-and-quality` suite (see
`.github/codeql/codeql-config.yml`). The repo filters out
`py/path-injection` because `tapscribe/sessions.py` already enforces a
strict two-layer path sanitiser — every OTHER rule still runs and
PR authors are expected to land clean. Common trip-wires Claude has
introduced and how to avoid them:

- **Path components from parsed filenames must round-trip through
  `safe_name`** before being interpolated into a new path. Even when
  the source filename came from a validated `session_dir.glob("*.wav")`
  walk, CodeQL's intraprocedural analysis can't see the upstream
  containment check. Both the live `/tap` recorder
  (`tapscribe/tap_fan_out.py`) and the strip-silence splitter
  (`tapscribe/sessions.py`) build filenames via
  `tapscribe.text.build_recorder_wav_name`, which applies `safe_name`
  internally — use that helper, don't build the filename yourself.
- **Subprocess argv must always be the list form** (`subprocess.Popen([
  exe, "--flag", value])`) — never `shell=True`, never an f-string.
  `tapscribe.live.build_live_cmd` is the only place that constructs
  argv; new flags go there, with each value `str()`-converted at the
  call site.
- **Never widen `query-filters` in the CodeQL config to silence a new
  finding.** Every exclude has a written justification block; the bar
  for a third one is high. Fix the code instead.
- **CLI parsers (argparse, etc.) flow into many places** — even though
  the operator launches the recorder, CodeQL treats argparse values as
  external input. Constrain with `choices=` / `type=int` / `type=float`
  where possible; defensive validation at the boundary keeps the rest
  of the codebase clean.
- **Every `except: pass` (and `except Foo: pass`) needs an explanatory
  comment inside the body** explaining *why* swallowing is the correct
  behaviour. CodeQL's `py/empty-except` flags bare passes, and the
  reviewer asks for justification on every PR that ships one. Don't
  ship `except Foo: pass` — write `except Foo:` then a comment saying
  what raises it, why suppression is intentional, and what's lost when
  it's swallowed. If suppression is *not* the right behaviour, log or
  re-raise instead.

## Verify before claiming green: actually run every CI job locally

CI has multiple jobs and they don't all run by default. **Skipped tests
do not count as passing tests.** Before claiming `pytest tests/e2e` is
green, check what's in the CI matrix you're not running:

- `pytest --ignore=tests/e2e ...` runs the Python unit suite.
- `pytest tests/e2e -k 'not real_whisper'` is **incomplete** on this dev
  box because `tests/e2e/test_dashboard_ui.py` silently skips itself
  (`pytest.skip(... allow_module_level=True)`) when playwright isn't
  importable. The CI job *does* install playwright and runs every test
  in that file. Without playwright locally you'll never see a real-DOM
  regression — the suite will just print `1 skipped`.
- The dashboard-UI CI job runs `pytest tests/e2e/test_dashboard_ui.py
  -q -m "not real_audio"`. Run the same locally before pushing.

### Local playwright setup

`pytest tests/e2e/test_dashboard_ui.py` skips itself if `playwright`
isn't importable. On a normal developer laptop the standard setup
works — `pip install playwright && python -m playwright install
chromium`. Only fall back to the recipe below when the standard
install fails (no outbound network, network policy blocks the Chrome
for Testing download, etc.), which is the typical case inside Claude
Code on the web's managed execution environment.

#### When `playwright install` can't reach the network (Claude Code on the web, etc.)

On the sandboxed dev box, Chromium ships pre-installed under
`/opt/pw-browsers/chromium-<rev>/`. Playwright matches its bundled
Chromium revision to the playwright package version, so version drift
between `pip install playwright` (latest) and the on-disk browser is
the standard failure mode ("Executable doesn't exist at
/opt/pw-browsers/chromium_headless_shell-<N>/...").

**As of 2026-05-21**, the on-disk revision is `1194`, which matches
**playwright 1.56.x**:

```bash
pip install 'playwright==1.56.*'
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
pytest tests/e2e/test_dashboard_ui.py -m 'not real_audio'
```

`playwright install chromium` will fail (no outbound network); don't
try. If the on-disk revision changes, mismatched-version errors tell
you which version Playwright is looking for — `ls /opt/pw-browsers/`
shows the actual revision, and bumping the pip install to the matching
playwright release brings them back in sync. Don't paste the recipe
above wholesale on a real developer laptop; it's only correct for the
managed environment.

### When CI flips red on a dashboard test, suspect import hygiene

The dashboard JS swallows exceptions thrown inside addEventListener
callbacks; a `ReferenceError` from a removed-but-still-used import will
*not* surface as a pageerror, the unit test suite will pass, and only
the playwright job catches it. After any refactor in
`tapscribe/web/js/components/session-detail.js` that touches the
top-of-file `import { ... } from "../templates.js"`, grep the file for
every name you removed (`grep -n '\bslot('`, etc.) before pushing —
helpers used by `buildExpandTx`, the regex tester, and other rarely
exercised paths are easy to miss when ripping unused-looking names.
