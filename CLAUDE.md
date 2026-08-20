# Pointers for Claude

- `CONTEXT.md` — domain glossary (Recorder, Bridge, Utterance, Drain, …).
  Use those names; don't introduce shadow vocabulary.
- `.claude/hooks/` — `session-start.sh` installs deps, `stop.sh` runs ruff.
  Convention changes go there or in `pyproject.toml`, not here.
- `bridges/README.md` — Bridge → `/tap` wire contract. The constants in it
  are declared in JS, C#, Python and prose, and are held in lock-step
  mechanically: `tapscribe/` is the SOURCE (edit `auth.py` /
  `speech_gate.py` / `audio.py` by hand), `python3 tools/stamp_tap_wire.py`
  writes every bridge and every doc that restates them, and
  `tests/test_tap_wire_contract.py` fails on drift — including a
  declaration site nobody listed. Adding a bridge in a new language is one
  new `Site` row; Python on the Recorder's side of the wire just imports.
  Never hand-edit a wire constant in `bridges/` — run the stamper. The
  Blip-resilience recipe (backoff / gap buffer / drain) is GATED but never
  stamped: the Recorder has no opinion on it. ADR-0019, incl. why this is a
  stamper and not codegen.
- `tapscribe/routes/` — the HTTP surface, one route module per resource
  group; its `__init__.py` docstring is the index ("where does X get
  served"). A new route goes in the module whose DOMAIN CONCERN it
  belongs to (not its URL prefix — see `strip.py`, which owns four
  routes across `/api/sessions/*` and `/api/wav/*` so the knob parser
  stays single-owner) and gets a line in that module's docstring route
  map. `tests/test_route_surface.py` fails if the map drifts, if
  `app.py` registers a route itself, if a route module imports a
  sibling route module (shared helpers live in `deps`/`body`/`errors`/
  `guards`), or if the registered surface changes without its golden
  table changing too. ADR-0018 has the why. Note `app.routes` no longer
  enumerates routes (FastAPI keeps an included router as one lazy
  entry, and the effective path of a websocket or mount lives on the
  context's `starlette_route`): a test that sweeps the surface goes
  through `tests/route_inventory.py`, which owns that traversal (and
  handles both sides of 0.139) rather than re-deriving it. A StaticFiles
  mount is the one thing that cannot ride a router: `include_router`
  carries a `Mount` only from 0.139 and silently drops it before that,
  so `routes/assets.py` declares mounts in `STATIC_MOUNTS` and attaches
  them to the app.
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
  selection mid-copy is the same bug as snapping a dropdown shut). A
  deferred swap lands on the NEXT poll pass via the tick-retry — the one
  retry mechanism every held render in the app uses (ADR-0016); see
  `spine.js` for the reference adoption; `summary.js` output pane and
  `transcript.js` merged pane render through it too, calling
  `markRegionStale(host)` to force the next render after a mutate /
  lazy-body load WITHOUT bypassing the guards. There is no force flag.
  A **keyed list** (rows keyed and updated in place, never swapped) is the
  other shape, and it has its own primitive: `renderList(host, items, {key,
  create, update, itemSig, sig})` plus `markListStale(host)`, which the three
  keyed lists (`recordings.js`' WAV list, `transcript.js`' per-WAV picker,
  `sessions.js`' rows) all render through. Canon `reconcileList` is NOT
  re-exported from `templates.js` — `renderList` is the only door, so a keyed
  list cannot be added un-held. It owns two rules a region has no equivalent
  for (the removal hold and the per-row hold) — see CONTEXT.md → Region ·
  keyed list, and don't re-derive either at a call site. Its
  empty/loading placeholder must be a **sibling** of the rows host, toggled in
  place, so the host's children belong to the seam alone.
  Per-tick
  updaters that mutate text/rows in place instead of rendering a region or a
  keyed list (the live log dialog, `active-taps.js`, `live-feed.js`) apply the
  exported `selectionInside(host)` for the same rule — defer WITHOUT updating
  the gate's signature, so the held-back render lands on the first
  tick after the selection clears. EVERY held render in the app lands through
  the `markDeferredRender`/`consumeDeferredRender` tick-retry — those BESPOKE
  gates, every `renderList` deferral, and every `renderRegion` deferral alike.
  One mechanism, no exceptions to remember; ADR-0016 has the why, including why
  a listener-based instant flush (which canon `lib/render.js` still implements
  and this seam no longer reaches) fits only one of the four render shapes.
  Corollary worth internalising before touching the seam: `renderRegion` checks
  its `sig` BEFORE the holds, so an idle focused control marks no retry — invert
  that order and every 304 tick re-runs `renderAll` for as long as a caret sits
  in a region (#245). `live-channel.js` and `config-card.js`
  render through it too, with NO bespoke guard of their own: a focused
  `[data-cfg-key]` save button mid-`putJson` counts as an interactive
  control in the seam's `_isInteractive`, so `renderRegion` holds the
  swap (and `interactionHeld()` reports it to the poll pacer) without
  the call site remembering anything. Both used to carry a hand-rolled
  2-line guard that returned WITHOUT marking the deferred render, which
  stranded the held-back render forever once the poll started 304ing —
  the argument for folding it into the seam rather than repeating it.
  The People editor (`people.js`) renders its list through
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
- Every lazily-fetched body on `/next` is a **lazy resource**
  (`web/js/lazy-resource.js` `createResource`, declared in `web/js/api.js`), and
  a view reads it by **binding a watcher once at build time**
  (`const body = resource.watch(() => { markRegionStale(host); afterMutate(); })`)
  and calling **`body.resolve([...key args])`** each tick — never through
  hand-rolled `peek`/`fetch` choreography. The resolve owns the whole per-tick
  state machine: peek-or-fetch-ONCE (in-flight keys deduped across the ticks
  before one lands, and across WATCHERS — two views waiting on one key share the
  fetch and each get their own land), the failure policy, and the
  stale-while-revalidate hold. It returns `{ value, loading, stale, error }`;
  `loading` is true only on a genuine COLD load, which is the one case a caller
  may paint a placeholder, and `stale` says `value` is the last-good body rather
  than this key's own — a render gate keyed on the SIGNATURE must include `stale`
  as a term, since held rows and that signature's own rows are otherwise the same
  sig and the swap between them would be skipped. Whoever owns the resolve owns
  that term's spelling, not each view: `session-files.js` returns a ready-made
  `sigTerm` (stamp + provisional-ness) that both WAV lists splice in place of
  `files_sig`, because a view spelling it differently — or omitting it — silently
  stops reconciling, which is the same class of bug as a forgotten `sig` dep. The
  rule is
  about the PER-TICK path: a one-shot, event-driven read still uses `fetch`
  directly, because it paints into one specific node and needs its own
  latest-wins guard rather than a watcher's repaint — `recordings.js`'
  `fillExpand` (a row's transcript expand, guarded by `host.dataset.txStamp`) is
  the one such case, and `wavTranscript` accordingly declares neither a hold nor
  a failure policy. The callback is bound at `watch` rather than passed per resolve for
  the same reason `createFieldSaver` takes `afterSave` at construction: waiting
  callbacks are deduped by identity, so a fresh closure per tick would repaint
  once per missed tick — binding makes that unwritable instead of merely
  documented. #222 was five copies of this machine with three
  divergent, unchosen failure policies; add a sixth lazy body by declaring a
  resource, never by re-rolling the ceremony. The mechanism is DOM-free and
  fetch-free (injected `load`), unit-tested under `node --test` in
  `lazy-resource.test.js`; `api.test.js` is the integration half over the real
  exported resources.
  Two things a resource DECLARES, both decisions rather than fallout:
  - `onFailure` — `retry-next-poll` (silent, retries on a later tick; for a body
    whose absence is transient and whose region has a placeholder) or
    `remember-error` (reports the rejection, does not refetch until the key
    changes; for a body whose absence is a property of the file — an unreadable
    WAV has no peaks, and re-asking every 500 ms answers nothing).
  - `holdKeyOf` — stale-while-revalidate, and it is REQUIRED when the key is a
    SESSION-LEVEL AGGREGATE that flips when any ONE sibling changes (the
    canonical case is `files_sig`, which flips when a single WAV of many is
    (re-)transcribed / stripped / added). Without the hold, the refetch reports a
    cold load, the view renders that exactly like a first load and
    `replaceChildren`-blanks the whole multi-item region to "loading…" once per
    sibling change — the "multi-track pages blink while transcribing" bug (#266).
    `holdKeyOf` names the thing the body belongs to (the session); `keyOf` names
    one VERSION of it. Omit it only when a stale body would be WRONG rather than
    merely old (peaks belong to one (WAV, byte size)). The three held bodies are
    `sessionFiles`, `sessionTranscript` and `sessionSummary`; a new lazy pane
    keyed on a content stamp inherits the requirement. Pin it with an
    identity-stamp e2e that stamps an UNRELATED row, flips the sig via a sibling,
    and asserts the stamped node survives the poll
    (`test_next_files_sig_flip_does_not_blank_wav_list`,
    `…_does_not_blank_transcript_picker`).
  `knownValue` is the third hook: an answer available from the args alone (an
  empty `files_sig` means no folder on disk, so fetching would 404), recorded as
  the last-good body like a fetched one — otherwise a later non-empty flip
  resurrects the pre-deletion rows as ghosts.
  What is left ON TOP of `resolve` for the WAV listing — the `null`-is-never-a-
  value rule and the four states a listing region can be in
  (`none`/`loading`/`rows`/`empty`, where a cold load must beat emptiness or the
  region says "nothing here" mid-fetch) — has ONE owner in
  `web/js/next/session-files.js` (`createFilesSource` + `listState`), which both
  WAV lists cross. It is DOM-free and unit-tested under `node --test`; the
  placeholder WORDING stays per view, since that is content, not behaviour.
  The DUAL requirement: a multi-item
  region gated on such an aggregate is a **keyed list** and must render through
  `renderList` (keyed, in-place) — a full `replaceChildren` rebuild on the sig,
  even WITHOUT a placeholder blank, still churns O(content) nodes + row
  listeners on every sibling/tick change. All three keyed lists are the pattern
  to copy: the two WAV lists pass a list-level `sig` (they have `files_sig`),
  `sessions.js` passes a per-row `itemSig` instead (chrome mounted once, rows
  keyed by id + structural bits, cells mutated per row — #312) because it has no
  cheap aggregate stamp to gate on.
- **Every save that shows a status** goes through `web/js/save-status.js`
  (`runSaveWithStatus`): `saving…` → a GUARDED promotion to `saved` (so a save
  settling late can't stomp a newer message) → auto-clear, or a `failed: …`
  that never auto-clears. `statusTarget(resolve)` writes the cells, re-resolved
  on every write because the card holding them is routinely rebuilt mid-save
  (a captured node is detached by then, making a `failed: …` invisible); the
  save BUTTONS (`wireSave` / `wireConfigSave`, which live there too — api.js
  stays the fetch layer) and `next/ui.js`'s `makeStatusFlasher` are all
  specialisations of it. #355 was four hand-rolled copies of this, already
  drifted on badge duration and guardedness; add a fifth trigger by writing a
  wrapper over `runSaveWithStatus`, never a new lifecycle.
- **Optimistic inline editing** on `/next` is `web/js/next/field-saver.js`'s
  two halves: `createOverlay({idOf, baselineFor})` holds the pending edits and
  owns the per-tick **catch-up sweep** (retire an entry once the server agrees
  — without it a stranded edit masks a later change made elsewhere forever),
  and `createFieldSaver({overlay, put, afterSave})` does one debounced PUT per
  id through the lifecycle above. `overlay.forget(id)` is the ONLY way to
  cancel a pending save (the saver re-reads the overlay at timer-fire), so
  there is deliberately no `cancel`. Both are generic: a new editable field
  BINDS them in one owner module rather than re-rolling either.
  `next/session-labels.js` is the reference (pending edit + one saver + the
  label read + the sweep; the Sessions list and the spine's rename card import
  verbs from it, never its Map), and its sweep runs once per tick from
  `main.js`'s `renderAll`, not per view. All DOM-free and unit-tested under
  `node --test` with `mock.timers`.

## Runtime deps the install picker does NOT cover

`tapscribe/install_picker.py` resolves *model* extras (`whisper-cpu`,
`parakeet-mlx`, …). Two runtime dependencies fall outside that matrix
and are wired into `tapscribe/preflight.py` instead, which
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

- **The speaker-embedding model** — diarization's CAM++ export
  (`tapscribe/diarizers/model.py`, ADR-0021) is **fetched**, not
  vendored: 30 MB whose sha256 the fetcher VERIFIES, where a committed
  blob is only trusted. The probe is "file present AND digest matches",
  so a truncated download reads as absent and is repaired rather than
  loading as a broken graph later. The repair runs the module
  (`python -m tapscribe.diarizers.model`), not pip, and is non-fatal:
  no model means a multi-person tap stays one speaker. Retrying every
  launch is deliberate here — an offline box fails in milliseconds and
  gets the model the first time it has connectivity. Lands under
  `BASE_DIR/models/` (gitignored), never in site-packages. The module is
  stdlib-only because preflight imports it, pinned by
  `test_the_diarize_model_probe_imports_no_third_party`.

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

The chunk-size knobs are dashboard-tunable (Settings → Advanced), resolved
`env > config file > default` at use-time: `TAPSCRIBE_PARAKEET_CHUNK_S` /
`TAPSCRIBE_PARAKEET_OVERLAP_S` (module constants `ENV_CHUNK_S` /
`ENV_OVERLAP_S` in `transcribers/_chunked.py`, shared by both Parakeet
adapters) over `config/parakeet-{chunk,overlap}-s.txt`. Every
operator-tunable setting belongs in the dashboard; the pattern for a new
one is a `_ConfigSpec` entry in `config_store.CONFIG_KEYS` (write-time
bounds check), a `config_store.resolve_knob` resolver at the use site, the
resolved value on `/api/state`, and a field in the Advanced card
(`web/components/next/views.html` + `next/views/settings.js`). Per-session
ACTIONS — the strip-silence knobs in `web/components/next/recordings.html`
— are a different shape: a POST, not a persisted knob.

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
