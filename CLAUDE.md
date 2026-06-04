# Pointers for Claude

- `CONTEXT.md` — domain glossary (Recorder, Bridge, Utterance, Drain, …).
  Use those names; don't introduce shadow vocabulary.
- `.claude/hooks/` — `session-start.sh` installs deps, `stop.sh` runs ruff.
  Convention changes go there or in `pyproject.toml`, not here.
- `bridges/README.md` — Bridge → `/tap` wire contract.
- `frontend/` — TypeScript-via-JSDoc gate for `tapscribe/web/js/`. The
  `stop.sh` hook silently skips when `frontend/node_modules/.bin/tsc` is
  absent (fresh worktree before `session-start.sh` has finished), so if
  you've changed JS and want a local sanity check, run `cd frontend &&
  npm install && npm run typecheck` once after a fresh clone. CI's
  `frontend-typecheck` job is the source of truth either way.
  `noUnusedLocals` is on — declared-but-unread locals (and stale
  imports) are hard errors. To opt out of the check for an intentionally
  unused binding (the canonical case is an async closure that mutates a
  variable consumed via a post-await cast — see `stripSession` in
  `main.js`), prefix the name with `_`.
- `/next` re-renders every per-tick region on each 500ms `/api/state`
  poll via `replaceChildren`, which would snap a focused `<select>`
  shut or drop a caret mid-edit. Any `/next` region that's rebuilt on
  the tick AND can hold an interactive control (`<select>`/`<input>`/
  `<textarea>`/contenteditable) MUST render through `renderRegion`
  (`web/js/templates.js`) rather than raw `replaceChildren` — it skips
  the swap while a control inside the host is focused OR while a text
  selection starts/ends inside it (clobbering a selection mid-copy is
  the same bug as snapping a dropdown shut; see `spine.js` for the
  reference adoption). Per-second updaters that write text in place
  instead of swapping (the live log dialog) use the exported
  `selectionInside(host)` for the same rule. `live-channel.js` and `config-card.js`
  render through it too, keeping only a 2-line `[data-cfg-key]` button
  guard renderRegion deliberately doesn't cover (a focused save button
  mid-putJson isn't an "interactive control" but must still hold the
  swap). The People editor's bespoke guard predates `renderRegion` and
  is left as-is; `renderRegion` is the pattern for NEW regions. The
  `test_next_poll_render_does_not_clobber_open_controls` sweep in
  `tests/e2e/test_dashboard_ui.py` enforces this — it focuses every
  control in each view, crosses a poll, and fails if a node is rebuilt
  out from under the focus, so a new unguarded dropdown trips CI.
- Render-signature hygiene on `/next`: a value that changes every tick or
  every second (job progress, byte counters, live captions) must update
  its DOM in place or carry its OWN small signature — never share a render
  signature with an O(content) region. A 3000-segment merged transcript is
  a 100–200 ms synchronous rebuild; sharing its sig with job progress was
  the "/next locks up while transcribing" bug (one stall per job tick).
  Two identity-stamp guards in `tests/e2e/test_dashboard_ui.py`
  (`test_next_job_ticks_do_not_rebuild_merged_transcript`,
  `test_next_caption_churn_appends_feed_lines_without_rebuilds`) pin the
  fixes structurally — no timing thresholds, so they hold on slow CI.
  To MEASURE render-path changes, run the opt-in CDP soak harness:
  `TAPSCRIBE_PERF_SOAK=1 pytest tests/e2e/test_next_perf_soak.py -s`
  (multi-pass scenarios; reports long tasks, poll health, post-GC
  node/listener/heap growth — see its module docstring for knobs).

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

The chunk-size knobs are env-tunable (`TAPSCRIBE_PARAKEET_CHUNK_S`,
`TAPSCRIBE_PARAKEET_OVERLAP_S`, `TAPSCRIBE_CANARY_CHUNK_S`,
`TAPSCRIBE_CANARY_OVERLAP_S`, `TAPSCRIBE_CANARY_MAX_TOKENS`); env
names are exported as module constants from each adapter
(`ENV_CHUNK_S`, `ENV_OVERLAP_S`) so the dashboard wiring — when it
lands — has one source of truth. Every operator-tunable setting
belongs in the dashboard eventually; see the strip-silence knobs in
`web/components/next/recordings.html` for the pattern when adding
these.

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

### The pre-push hook will stop a red push for you

`.claude/hooks/pre-push.sh` (wired into `.claude/settings.json` as a
`PreToolUse` hook on `Bash`) inspects every Bash command Claude is
about to run. When the command actually invokes `git push`, the hook
runs the same two checks CI's `tests` job runs — `ruff check
tapscribe tools tests bridges/local-test-bridge` and `pytest tests` —
and exits **2** if either is red, blocking the push and feeding the
failure back to Claude. Anything that isn't a `git push` is passed
through with exit 0, so the rest of the session pays no overhead.

Don't treat the hook as a substitute for running the suite yourself
while iterating: a 37-second wait at push time is *much* longer than
the 5-second feedback loop of running pytest as you change code, and
each blocked push wastes a turn. Run `pytest tests` before you call
the work done; let the hook catch the cases you missed.

Genuine emergency bypass: prefix the push with `CLAUDE_SKIP_PRE_PUSH=1`
(e.g. mid-debug branch reset where the working tree is intentionally
broken). It's an audible escape, not a silent one — don't reach for it
just because the gate is annoying.

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
the playwright job catches it. After any refactor in a Stages view
(`tapscribe/web/js/next/views/*.js`) that touches the top-of-file
`import { ... } from "../../templates.js"`, grep the file for every
name you removed (`grep -n '\bslot('`, etc.) before pushing — helpers
used by expand/download/delete handlers and other rarely exercised
paths are easy to miss when ripping unused-looking names. (`tsc`'s
noUnusedLocals catches the opposite case — an import left behind.)
