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
- `/next` re-renders every per-tick region on each `/api/state`
  poll (500ms, adaptively backing off to 2s when idle — ADR-0013)
  via `replaceChildren`, which would snap a focused `<select>`
  shut or drop a caret mid-edit. The governing rule is the
  **Interaction hold** (CONTEXT.md + ADR-0004): defer the render, never
  destroy interaction state, and never advance the render gate on a
  skip. Any `/next` region that's rebuilt on
  the tick AND can hold an interactive control (`<select>`/`<input>`/
  `<textarea>`/contenteditable) MUST render through `renderRegion`
  (`web/js/templates.js` — the app seam over the vendored vanilla-web
  canon in `web/js/lib/render.js`; never edit `web/js/lib/*` in place)
  rather than raw `replaceChildren` — it skips the swap while a control
  inside the host is focused, while a popover/`<dialog>` inside the host
  is open, OR while a text selection starts/ends inside it (clobbering a
  selection mid-copy is the same bug as snapping a dropdown shut), and a
  deferred swap flushes ITSELF the instant the hold clears (one-shot
  listener per host — no next poll tick needed); see `spine.js` for the
  reference adoption; `summary.js` output pane and `transcript.js` merged
  pane render through it too, calling `markRegionStale(host)` (now canon,
  upstreamed) to force the next render after a mutate / lazy-body load
  WITHOUT bypassing the guards — never `force:true`, which would clobber
  a mid-copy selection). Per-tick
  updaters that mutate text/rows in place instead of swapping a region (the
  live log dialog, `active-taps.js`, `live-feed.js`) and the `recordings.js`
  WAV-list view-level gate (it gates the whole view body, not a single host
  swap, so it can't use `renderRegion`) apply the exported
  `selectionInside(host)` for the same rule — defer WITHOUT updating
  the gate's signature, so the held-back render lands on the first
  tick after the selection clears (these BESPOKE gates are the only
  remaining users of the `markDeferredRender`/`consumeDeferredRender`
  tick-retry; canon `renderRegion` deferrals no longer need it). `live-channel.js` and `config-card.js`
  render through it too, keeping only a 2-line `[data-cfg-key]` button
  guard renderRegion deliberately doesn't cover (a focused save button
  mid-putJson isn't an "interactive control" but must still hold the
  swap). The People editor (`people.js`) renders its list through
  `renderRegion` too — `renderRegion` is the pattern for every region,
  new or existing. The
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
  The dual of that footgun: `renderRegion(host, fn, {sig})` makes you
  hand-maintain `sig` to list every value `fn` reads — forget one and
  the region silently goes **stale** (never rebuilds). So for a DERIVED,
  non-interactive display bit (a staleness badge, a count) prefer
  rendering it as a SIBLING toggled in place each tick (like the
  `active-taps.js`/`live-feed.js` in-place updaters) rather than inside a
  sig-gated `renderRegion` pane — that sidesteps the drift entirely (the
  `summary.js` #94 stale-summary cue uses this pattern). When you DO use a
  sig-gated pane, a `renderRegion` sig-drift **audit** catches a missing
  dep: set `globalThis.__TAPSCRIBE_SIG_AUDIT = true` (dev console, or a
  test) and skipped regions are re-built into a probe and compared; any
  region whose output drifts from its sig is recorded to
  `globalThis.__TAPSCRIBE_SIG_DRIFT`. The
  `test_renderregion_sig_audit_finds_no_drift` e2e test enables it across
  the views and asserts no drift.
- Stale-while-revalidate for lazy, sig-keyed listings on `/next`: a
  lazily-fetched, signature-keyed resource (`web/js/api.js` `_resource`)
  whose key is a SESSION-LEVEL AGGREGATE that flips when any ONE sibling
  changes — the canonical case is `files_sig`, which flips when a single
  WAV of many is (re-)transcribed / stripped / added — must NOT return a
  bare "loading" sentinel (`null`) on the refetch. The view renders that
  sentinel exactly like a COLD first load and `replaceChildren`-blanks the
  whole multi-item region to a "loading…" placeholder, once per sibling
  change — the "multi-track pages blink while transcribing" bug (#266).
  Hold the last-good value during the refetch (stale-while-revalidate) so
  the region reconciles in place; return the cold-load sentinel ONLY when
  nothing was ever resolved for that session. `loadSessionFiles` is the
  reference: a per-session `_lastGoodFiles` cache returns the previous
  listing while the new `files_sig` refetches, so the Recordings +
  Transcript WAV lists (both `reconcileList`-backed) refresh in place
  instead of flashing. Pin it with an identity-stamp e2e that stamps an
  UNRELATED row, flips the sig via a sibling, and asserts the stamped node
  survives the poll (`test_next_files_sig_flip_does_not_blank_wav_list`,
  `…_does_not_blank_transcript_picker`). The same shape recurs for any
  region fed by a lazy body keyed on a content stamp — the merged
  transcript pane (`transcript.js`, `sessionTranscript`) and the summary
  output pane (`summary.js`, `sessionSummary`) blank to "loading…" on a
  re-transcribe / external re-summarize for the same reason; give them a
  last-good hold if that blink matters. The DUAL requirement: a multi-item
  region gated on such an aggregate must render through `reconcileList`
  (keyed, in-place) — a full `replaceChildren` rebuild on the sig, even
  WITHOUT a placeholder blank, still churns O(content) nodes + row
  listeners on every sibling/tick change. The WAV lists AND `sessions.js`
  (#312: chrome mounted once, rows keyed by id + structural bits, cells
  mutated via a per-row sig with focused-control guards) are the pattern
  to copy — `sessions.js`'s old whole-list `listSig` swap was the last
  offender.

## Runtime deps the install picker does NOT cover

`tapscribe/install_picker.py` resolves *model* extras (`whisper-cpu`,
`parakeet-mlx`, …). One recurring runtime dependency falls outside
that matrix and is wired into `tapscribe/preflight.py` instead, which
`start.sh` / `start.ps1` and the Bundle's Launcher all run after the
picker:

- **`onnxruntime`** — the per-tap TapScribe gate (`gate_kind="tapscribe"`,
  the default) runs the VAD through `tapscribe.vad`, a numpy+onnxruntime
  port of Silero over a vendored model (`tapscribe/vad/silero_vad.onnx`,
  see its `PROVENANCE.md`), imported lazily on the first `/tap` WS.
  Missing → the tap logs `gate construction failed … falling back to
  passthrough` and the gate the operator picked is silently a no-op. It
  is a **core** dependency (no `[vad]` extra — `pyproject.toml`'s
  `dependencies` list installs it unconditionally with a plain
  `pip install -e .`), so `preflight.plan_steps`' `find_spec` probe +
  reinstall step is a repair for an incomplete venv; on a healthy
  install it's already satisfied and no step is planned.

  **The `silero-vad` package and `torch` are NOT dependencies** — #374
  dropped both (773 MB) in favour of the vendored model. A probe here
  must name a module some core dependency actually provides, or the
  reinstall it plans can never satisfy it and preflight re-runs pip on
  every launch; `test_every_core_repair_probes_a_module_a_core_dependency_provides`
  pins that. The differential test that keeps the port honest,
  `tests/test_vad_silero_port.py`, needs the real upstream package and
  therefore has its own `upstream-contract` CI lane.

**`ffmpeg` is NOT required, period.** Every array-accepting backend
(`mlx_whisper`, `mlx_parakeet`, and the `transformers` Parakeet path in
`parakeet.py`) pre-decodes the recorder's WAV via
`tapscribe.wav_predecode.load_recorder_wav_as_pcm` into a numpy float32
array and hands it directly to the model's array-accepting entry point:

- `mlx_whisper.transcribe(array, …)` for Whisper. The model's own
  30-s internal windowing covers long inputs.
- `parakeet_mlx.audio.get_logmel(mx.array(array), preproc)` →
  `model.generate(mel)` for MLX Parakeet, **inside a chunking loop**
  (`chunk_duration_s` / `overlap_duration_s` knobs on
  `MlxParakeetTranscriber`) so long sessions stay under the Metal
  GPU's per-buffer cap. Per-window timestamps are shifted by the
  window's start so the merged result is session-relative.
- `processor([array], sampling_rate=16000)` →
  `AutoModelForTDT.generate(..., return_dict_in_generate=True)` →
  `processor.decode(sequences, durations=…)` for `transformers`
  Parakeet (CUDA/CPU), **also chunked** through the same
  `chunking.chunk_windows`. The decode returns per-token timestamps;
  `_parakeet_tdt.build_segments_from_tdt_tokens` folds them into
  word/segment alignment (a leading-space token starts a new word).
  Note: the high-level ASR *pipeline*'s `return_timestamps="word"` is
  CTC-only and raises on a TDT transducer — use the lower-level path.

The pre-decode trick lives in its own module
(`tapscribe/wav_predecode.py`) so the next contributor poking at
"where do we skip ffmpeg" finds it on the first grep. The chunking
skeleton (pre-decode → windows → per-window model call →
overlap-midpoint stitch) lives ONCE in
`transcribers/_chunked.ChunkedTranscriber`; each Parakeet adapter
implements only `_transcribe_window`.

There is **no path-based fallback**. Non-recorder WAVs (different
sample rate / channel layout / sample width) raise a clear
`RuntimeError("unexpected WAV format …")` at pre-decode time — the
operator's signal to convert the file, not a cue to silently
re-introduce ffmpeg. Reintroducing a `model.transcribe(str(path))`
fallback would defeat the whole point and is rejected at review.

The chunk-size knobs are env-tunable (`TAPSCRIBE_PARAKEET_CHUNK_S`,
`TAPSCRIBE_PARAKEET_OVERLAP_S` — shared by both Parakeet adapters); env
names are module constants in `transcribers/_chunked.py` (`ENV_CHUNK_S`,
`ENV_OVERLAP_S`) so the dashboard wiring — when it lands — has one
source of truth. Every operator-tunable setting
belongs in the dashboard eventually; see the strip-silence knobs in
`web/components/next/recordings.html` for the pattern when adding
these.

If a new runtime dep with the same shape (system binary, or optional
Python package gated by a lazy import) lands, add it as a `Step` in
`tapscribe/preflight.py`'s `plan_steps` — NOT inline in `start.sh` /
`start.ps1`. Operators still hit it once on bring-up instead of
mid-request, but the Windows Bundle's Launcher has no `start.ps1` to
inherit it from and runs `python -m tapscribe.preflight` instead
(ADR-0015), so a shell-inlined probe silently never runs there.
`plan_steps` is pure and its probes are injected, so a new step is
unit-testable without a GPU, without the dep installed, and without
running pip.

### Upstream adapter symbols: lock the contract with a smoke test

Several adapters import undocumented or version-volatile symbols from
upstream packages — `mlx_whisper`, `parakeet_mlx.audio`, the
`mlx_voxtral` port, and `transformers` (`AutoModelForTDT` +
`processor.decode(durations=…)` for Parakeet). Upstream renames silently
break those imports at request time, *after* a clean unit-test run that
mocked the model object. The convention to catch this earlier:

- **Pin a narrow upper bound** in `pyproject.toml`
  (`transformers>=5.12,<6`, `parakeet-mlx>=0.5,<0.6`, etc.) — primary
  defence.
- **Add an `importorskip`-gated smoke test** that asserts every
  imported symbol exists with the expected shape (signature kwargs,
  classmethod presence). One per adapter, at the bottom of its
  `tests/test_transcribers_*.py`. The test no-ops on hosts where the
  upstream package isn't importable (e.g. the Linux CI matrix for the
  MLX adapters) and runs in full where it is.

A future upstream API change then fails CI on the PR that bumps the
pin instead of failing in production weeks later. The pattern is
short — see `test_transformers_parakeet_upstream_contract` in
`tests/test_transcribers_parakeet.py` for the template.

## Security: avoid CodeQL "security-and-quality" tripwires

PRs are scanned by CodeQL with the `security-and-quality` suite (see
`.github/codeql/codeql-config.yml`). The repo filters out
`py/path-injection` because `tapscribe/session_paths.py` already enforces
a strict two-layer path sanitiser (`_safe_part`, then
`_assert_contained`/`_assert_under`) — every OTHER rule still runs and
PR authors are expected to land clean. That exclusion is what makes
`session_paths.py` a review-gate module: a new user-input-to-path helper
there is covered by neither CodeQL nor any other automated check.
Common trip-wires Claude has introduced and how to avoid them:

- **Path components from parsed filenames must round-trip through
  `safe_name`** before being interpolated into a new path. Even when
  the source filename came from a validated `session_dir.glob("*.wav")`
  walk, CodeQL's intraprocedural analysis can't see the upstream
  containment check. Both the live `/tap` recorder
  (`tapscribe/tap_fan_out.py`) and the strip-silence splitter
  (`tapscribe/batch_strip.py`) build filenames via
  `tapscribe.text.build_recorder_wav_name`, which applies `safe_name`
  internally — use that helper, don't build the filename yourself.
- **Subprocess argv must always be the list form** (`subprocess.Popen([
  exe, "--flag", value])`) — never `shell=True`, never an f-string.
  Argv construction is centralised per concern, and a new flag goes in
  the matching builder — never hand-rolled at a call site:
  `tapscribe.live.build_live_cmd` (the live channel),
  `tapscribe.install_target.pip_install_argv` (every pip invocation —
  checkout / Bundle wheel / pinned PyPI, ADR-0015),
  `tapscribe.preflight.plan_steps` (bring-up steps), and
  `RecorderCommand` (the Bundle Launcher's children). Each value is
  `str()`-converted at the call site.
- **Never widen `query-filters` in the CodeQL config to silence a new
  finding.** Every exclude has a written justification block; the bar
  for a third one is high. Fix the code instead.
- **CLI parsers (argparse, etc.) flow into many places** — even though
  the operator launches the recorder, CodeQL treats argparse values as
  external input. Constrain with `choices=` / `type=int` / `type=float`
  where possible; defensive validation at the boundary keeps the rest
  of the codebase clean.
- **A model / repo id from a request body must be validated against an
  allowlist before it reaches a model loader or a Hub download.** The
  summarizer model dropdown (`POST /api/sessions/{s}/summarize` body
  `model`) is untrusted input that flows into `mlx_lm.load` /
  `Llama.from_pretrained` — i.e. a network fetch keyed on attacker-
  controllable text. `tapscribe.summarizers.SUMMARY_MODELS` is the ONE
  curated catalog AND the allowlist: `is_allowed_local_model` rejects
  anything not listed (the operator's `TAPSCRIBE_SUMMARIZE_*_MODEL` env
  override and the bundled default are the only exceptions — operator-
  controlled, not external input). Add a model = add a `SummaryModel`
  row there; never let a raw body string reach a loader. The same
  catalog is what `GET /api/summarize/models` serialises for the
  dropdown, so the UI can only ever offer loadable, allowed choices.
- **Every `except: pass` (and `except Foo: pass`) needs an explanatory
  comment inside the body** explaining *why* swallowing is the correct
  behaviour. CodeQL's `py/empty-except` flags bare passes, and the
  reviewer asks for justification on every PR that ships one. Don't
  ship `except Foo: pass` — write `except Foo:` then a comment saying
  what raises it, why suppression is intentional, and what's lost when
  it's swallowed. If suppression is *not* the right behaviour, log or
  re-raise instead.

## Frontend toolkit vendoring + gates

The frontend vendors parts of the Verktoykasse toolkit (vanilla-web /
vanilla-components) as provenance-stamped, copy-verbatim files — **never
edit them in place**; re-copy from the toolkit to update:

- `tapscribe/web/js/vc/` — the vanilla-components library, ONE copy shared
  by `/setup` and the dashboard (its `PROVENANCE.md` has the re-copy
  commands); the shared token sheets live at `tapscribe/web/tokens.css` +
  `tones.css` (canon NAMES — `dashboard.css` overrides the VALUES to
  TapScribe's palette).
- `tapscribe/web/tools/` — the gate tier: `check-conventions.mjs`
  (signal-listener / html-string / raw-swap rules), `check-slots.mjs`
  (the `.html` `<template>`/`[data-slot]` ↔ JS `tpl()`/`slot()`/`pick()`
  seam) and `check-css-vars.mjs` (a required `var(--x)` defined nowhere).
  Each scans `tapscribe/web/` (the directory holding `tools/`). CI runs
  them in the `frontend-gates` job; the stop hook runs them locally.
  Suppressions are comment-borne and must carry a WHY — `// static-render`
  for a deliberate one-shot render, `// gate-allow: <rule> — reason`
  trailing on the line, or file-level in the first ~10 lines. Same bar as
  `except: pass` above: never a bare marker.
- `bridges/spacialchat-bridge/{lib,components}/` — an older stamped copy
  (`@48bf2bf`), deliberately left "stale" until its own re-vendor pass
  (re-vendoring changes component APIs and needs the bridge e2e).

Drift check (local-only — it needs a Verktoykasse checkout):
`node tapscribe/web/tools/check-vendored.mjs <toolkit-checkout>` from the
repo root. `stale` is fine (canon moved on; it prints the exact re-copy
command); `forked` is the violation — never patch a vendored copy, extend
around it or upstream the change (see Verktoykasse #70 for the shape).

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
- The **headed bridge `browser_e2e`** (`test_bridge_extension_e2e.py` +
  `test_bridge_meeting_e2e.py`) load the real MV3 extension in a *headed*
  Chromium, which needs a display — without one they self-skip ("needs a
  display — run under xvfb"; the skip is honest and does NOT mask a real
  launch failure when a display IS present). `pytest tests` therefore skips
  them on a plain box. Run them under a virtual display:
  `xvfb-run -a python -m pytest tests/e2e/test_bridge_extension_e2e.py
  tests/e2e/test_bridge_meeting_e2e.py -m browser_e2e -q` (the meeting flow
  also needs `pip install -e ".[whisper-cpu]"`). CI runs both in the
  `bridge E2E (extension + meeting)` job.

### e2e selectors: `data-slot` is the test-id; open Playwright via `playwright_session()`

Browser tests open Playwright through `playwright_session()`
(`tests/e2e/harness.py`), NOT raw `async_playwright()`. The wrapper points
Playwright's test-id attribute at `data-slot` — the native `data-*` marker the
dashboard templates already bind through (`slot()`/`pick()` in
`web/js/templates.js`), so `page.get_by_test_id("waveName")` resolves
`[data-slot="waveName"]` with auto-waiting. Existing `[data-slot=…]` CSS
locators still work; `get_by_test_id` is the additive entry point. There is no
native HTML `testid` — `data-*` is the platform's scriptable-handle mechanism,
so this is the native convention, not a borrowed framework idiom. New e2e files
import `playwright_session` from `.harness` rather than reintroducing
`async_playwright()`. Prefer accessible selectors (`get_by_role` /
`get_by_label`) for controls and `data-slot` only for structural seams that have
no role; never select on presentational classes (`.wavrow__dur`, `.segctl__opt`)
— they break on a restyle. `test_get_by_test_id_is_wired_to_data_slot` pins the
wiring. The full convention + copyable configs (JS + Python) live in the
vanilla-web skill (`reference/testing.md`, `testing/`).

### The pre-push hook will stop a red push for you

`.claude/hooks/pre-push.sh` (wired into `.claude/settings.json` as a
`PreToolUse` hook on `Bash`) inspects every Bash command Claude is
about to run. When the command actually invokes `git push`, the hook
runs the same three checks CI's `tests` job runs — `ruff check` and
`ruff format --check` over `tapscribe tools tests benchmarks
bridges/local-test-bridge`, plus `pytest tests` —
and exits **2** if any is red, blocking the push and feeding the
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
