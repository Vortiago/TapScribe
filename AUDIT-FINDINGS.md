# Whole-codebase audit — findings not yet applied

Produced by `/code-review max --fix` over the whole repo: 16 parallel reviewer
agents (one per subsystem, plus cross-cutting passes for unit-test quality,
e2e/regression coverage, comment accuracy, and a `matt-code-review` Standards
axis), then a lead verification pass.

**Applied and committed** (see `git log main..HEAD`):

1. `fix(session-paths)` — two containment escapes + the CodeQL gate re-point.
2. `fix(preflight)` — the unsatisfiable silero-vad probe, the VAD's missing CI
   coverage, and the stale torch/silero doc cluster.
3. `fix:` — seven correctness bugs where the right guard already existed nearby.

Everything below is **reviewed and NOT applied**. Each was either judged too
behaviour-changing to auto-apply, needs work outside the reviewed spot, or
touches vendored canon that repo convention forbids editing in place.

---

## A. Blocked on the vendored-canon rule — needs upstreaming to Verktoykasse

`CLAUDE.md`: never edit `web/js/lib/*` or `web/js/vc/*` in place. Both of these
were **verified in headless Chromium** against the vendored file.

### A1. `renderRegion`'s deferral flushes on a focus move WITHIN the same host
`tapscribe/web/js/lib/render.js:122`

The one-shot `focusout` listener re-enters `renderRegion`, but during
`focusout` `document.activeElement` is `<body>` — so the re-checked guard sees
no hold and the swap lands on top of the *incoming* focus.

Verified: host `<div><input a><input b></div>`; focus `a`, renderRegion defers,
Tab to `b` → the host is rebuilt and `document.activeElement === <body>` — `b`
was removed mid focus-transfer, so focus is lost entirely.

Impact: `live-channel.js`'s body region holds the model `<select>`, langInput,
gateKindSelect, four number inputs and the init-prompt textarea in ONE host. An
operator editing gate knobs while the channel transitions starting→running tabs
to the next knob and the whole form is rebuilt from server values — un-Applied
edits lost. **This is the ADR-0004 bug class the primitive exists to prevent.**

Upstream fix: use the `focusout` event's `relatedTarget` (available during
focusout, unlike `activeElement`) — flush only when
`!e.relatedTarget || !host.contains(e.relatedTarget)`, else re-arm.

### A2. `selectionInside()` misses a selection that SPANS the host
`tapscribe/web/js/lib/render.js:42`

Only `anchorNode`/`focusNode` are tested, so a Range starting before the host
and ending after it is not detected. Verified: the region was swapped and the
live selection lost the host's content out of its middle. Triggered by dragging
a selection across a whole panel to copy it; the next 500 ms poll tears the
region out.

Leaks into every consumer — `templates.js` re-exports the predicate for the
bespoke gates (`deferIfSelectionInside`, `_holdInside`, the live-log dialog,
`active-taps.js`, `live-feed.js`).

Upstream fix: add `range.intersectsNode(host)` per `sel.getRangeAt(i)`, keeping
endpoint containment as the fast path.

### A3. The conventions gate skips files by BASENAME
`tapscribe/web/tools/check-conventions.mjs:43` (vendored)

Verified by replaying the tool's own file selection: `js/templates.js` and
`js/next/shell.js` are dropped from the scan, because their basenames collide
with the canon's root-level files. So **the app seam itself and the module every
Stages view imports are exempt from all three rules**, and the `frontend-gates`
job is green for the wrong reason. `next/shell.js` already contains four
constructs the rules exist to catch (`replaceChildren` at :82 and :217 with no
`// static-render` WHY; `addEventListener` with no `{signal}`/`{once}` at :184
and :243).

In-repo fix that does NOT touch canon: rename `js/next/shell.js` →
`js/next/stage-shell.js` and update its ~8 importers, then address what the gate
surfaces. Upstream fix: make the exemption path-anchored, not basename-matched.

**Note:** this rename is the import-hygiene trap CLAUDE.md calls out — a missed
importer throws a `ReferenceError` that the swallowed-`addEventListener` path
hides everywhere except the playwright job. Do it with `npm run typecheck` plus
`test_dashboard_ui.py`, not the Python suite.

### A4. The html-string rule misses `innerHTML +=` and `setHTMLUnsafe()`
`tapscribe/web/tools/check-conventions.mjs:111` (vendored)

`/\.innerHTML\s*=(?![=])/` cannot match `innerHTML +=`. Verified: none of
`el.innerHTML += "<b>x</b>"`, `el.outerHTML += y`, `el.setHTMLUnsafe(s)` match
any rule. `innerHTML +=` is the natural way to append to a log/feed pane, and
this rule is the repo's XSS boundary for untrusted transcript/summary text.

Upstream fix: `/\.(inner|outer)HTML\s*(\+)?=(?![=])/` plus
`/\bsetHTMLUnsafe\s*\(/`.

---

## B. Frontend correctness — not applied (needs tsc + playwright to verify)

- **`sessions.js:478`** — `fillRow` advances the per-row render gate BEFORE its
  focus-guarded writes, so an update skipped because the rename input or absorb
  `<select>` is focused is lost permanently: on blur nothing re-runs, because
  the next tick recomputes the same sig and early-returns. The exact ADR-0004
  trap ("never advance the render gate on a skip"). A later keystroke then
  persists the stale value over the external rename.
- **`summary.js:459`** — `shown = lastSummary || resolveStored(...)`, cleared
  only on session switch, so a summary regenerated by the end-of-meeting
  pipeline or another tab is never displayed; the operator keeps reading the
  superseded one with no cue.
- **`summary.js:170`** — `resolveStored` returns a bare `null` on REFETCH, so
  the output pane blanks to the "No summary yet" empty-state on an external
  re-summarize (#266 shape). **`transcript.js` has since been fixed
  (`lastGoodMerged`), so CLAUDE.md's stale-while-revalidate paragraph naming it
  is now out of date — correct that when fixing this.**
- **`recordings.js:780` and `summary.js:425`** — `chromeSig`/`headSig` omit the
  session label their headers render, so a rename elsewhere leaves a stale
  label until an unrelated input changes. Bespoke gates, so the
  `__TAPSCRIBE_SIG_AUDIT` drift audit cannot see them.
- **`spine.js:34`** — `persistLabel` captures `statusEl` at build time, but the
  blur flush replaces the card before the 600 ms debounce fires, so a FAILED
  rename PUT reports into a detached node and the operator believes it saved.
  `people.js:76-89` already shows the fix (resolve the element at timer-fire).
- **`live-channel.js:47` / `config-card.js:83`** — the bespoke `[data-cfg-key]`
  hold returns WITHOUT `markDeferredRender()`, so a held-back render is
  stranded forever once the poll goes quiet (main.js skips renderAll on a 304
  unless a deferral was recorded). config-card is worse: it stamps
  `dataset.cfgKey` on the textarea too, so any focus strands the whole grid.
- **`setup.js:212`** — the SSE read loop has no try/catch and the caller does
  `void runInstall(install)`, so a stream break leaves the first-run wizard
  stuck on the spinner forever, on the pre-install surface where no other UI
  exists.
- **`api.js:76`** — `_capCache` evicts by first-insertion and `Map.set` on an
  existing key doesn't reorder, so the HOT session is the one evicted;
  returning to it after 64 others resurrects the #266 blank-on-sig-flip. Fix:
  `delete` then `set` to make it MRU before capping.
- **`formatters.js:22`** — rounding happens after the unit is chosen:
  `fmtDur(119.96)` → `"1m 60.0s"`, `fmtBytes(1048570)` → `"1024.0 KB"`
  (verified under node). Reachable from live counters every meeting
  (`active-taps.js` renders both per ~500 ms poll). Also `fmtDur(Infinity)` →
  `"Infinitym NaNs"`. **`formatters.js` has no co-located test at all** despite
  being pure and DOM-free — the repo's own stated criterion for `node --test`.
- **`live-channel.js:179`** — `initRow.open = !!lp.length` re-forces the
  `<details>` state on every rebuild, undoing the operator's manual
  collapse/expand. `renderRegion`'s overlay guard covers `:popover-open` and
  `dialog[open]` but not `details[open]`.
- **`templates.js:113`** — `_isInteractive`/`_holdInside` hand-mirror canon
  privates. Fixing either canon bug above makes the app copy disagree, and
  `test_renderregion_sig_audit_finds_no_drift` starts reporting PHANTOM drift.
  Upstream a public `holdInside`/`isInteractive` and delete both copies.
- **`sessions.js:629`** — search-mode rebuilds every hit row per tick with no
  sig gate, and its `// static-render` WHY ("cold, discrete mode switch") is
  factually wrong.

---

## C. Python correctness — reviewed, not applied

Ordered by severity. Each is contained; they were left out to keep the applied
diff reviewable, not because they are wrong.

- **`app.py:1468`** — `DELETE /api/sessions/{s}/stripped` has no in-flight-job
  guard and rmtree's the directory a running strip job is writing into
  (verified against a TestClient: returns 200 with a `strip` job claimed).
  It is the only destructive session route with no guard AND no test; the
  parametrised sweep covering the other four omits it. Also check-then-act.
- **`app.py:1458`** — a body-less `PUT /api/summarize/config` silently wipes
  the saved config and returns `{"ok": true}` (`_json_body` turns a parse
  failure into `{}`, and the writer has full-object clear semantics).
  `api_tap_new_session` guards this exact hazard explicitly.
- **`app.py:1959`** — an unknown `model`/`backend` escapes as a bare 500
  (`REGISTRY.require`'s KeyError isn't in `_DOMAIN_ERROR_STATUS`). Every
  transcribe route test monkeypatches a stub, so the real registry path is
  never exercised from HTTP.
- **`wav_cache.py:556`** — a failed legacy-sidecar migration leaves an empty
  `<wav>.transcripts/` that permanently shadows the still-present legacy
  `.json`; `_migrate_legacy_if_needed` early-returns on `d.exists()`, so it
  never retries. The operator's transcript is on disk but gone from the
  dashboard, forever. (`mkdir` happens before the move.)
- **`wav_cache.py:511`** — `_primary_filename` stats paths from an
  already-taken glob, so a concurrent delete makes `FileNotFoundError` escape
  into the 500 ms `/api/state` poll.
- **`wav_cache.py:391`** — `hallucination_rules` is not in the cache match key,
  so editing the filter and re-running Transcribe session returns the
  transcript filtered under the OLD rules, byte-identical, with no signal.
  Cheap fix: re-run `hallucinations.apply` over the persisted
  `segments + suppressed_hallucinations` on a cache hit — no model runs.
- **`wav_append.py:86`** — `prior_data_bytes` is rounded to the byte, not the
  frame, so an odd-length prior WAV (a partial flush on ENOSPC) makes the
  resume append land off the sample grid: every appended sample decodes as
  high-byte-of-one + low-byte-of-next, i.e. **loud noise for the rest of the
  utterance**. The existing test truncates by a whole frame.
- **`session_maintenance.py:305`** — `absorb_session` merges meta aliases but
  never carries `session-roster.json`, then rmtree's the source. Filenames keep
  only `safe_name(identity)[:10]`, so ADR-0009's cross-session join key is
  irrecoverably lost. (`:326` likewise leaves a stale `session-transcript.txt`
  while reporting `transcript_invalidated`.)
- **`live.py:703`** — `matches()` compares `gate_kind` raw while the dashboard
  is shown the `effective_gate_config`-coerced value, so echoing it back forces
  a full restart for a knob tweak and then permanently overwrites the
  operator's `backend` choice with `tapscribe` — the invariant
  `test_operator_config_survives_a_moonshine_roundtrip` protects.
- **`live.py:846`** — `start()` holds `self._lock` across a multi-hundred-MB
  HuggingFace download while `stop()` blocks on lock *acquisition*, where its
  `timeout` has no effect. Ctrl-C wedges ASGI shutdown for the whole download.
- **`batch_transcribe.py:373`** — `transcribe_one` never claims the session job
  slot, so the per-WAV re-transcribe runs concurrently with a pipeline: two
  covers resident at once, and `set_primary_transcript` races on the same WAV.
  `batch_pipeline`'s own docstring promises a 409 here.
- **`config_store.py:142`** — `_check_batch_model` accepts a LIVE-ONLY id, so a
  Bridge-triggered pipeline dies at the transcribe stage with a raw
  `NotImplementedError`. `live_control.plan_live` already applies the stricter
  `supports_context` check on its side.
- **`hallucinations.py:146`** — `apply` moves matched segments out of
  `.segments` but never recomputes `.text`, so the sidecar persists the
  suppressed hallucination in its joined text (visible now as a wrong word
  count in the Transcript view; latent as a re-introduction vector).
- **`summarizers/local.py:102`** — the GGUF path loads `n_ctx=8192` while the
  catalog advertises `context_tokens=128_000` for the same row, and nothing
  budgets the transcript. A one-hour meeting raises llama-cpp's `ValueError`
  outside the wrapped block → bare 500. Note `MAX_TOKENS_BOUNDS[1]` equals
  `_DEFAULT_GGUF_CTX`, so the top of the allowed output range leaves zero input
  room.
- **`roster.py:104`** — the bridge-supplied `?name=` is persisted unbounded and
  unsanitised, then folded verbatim into the summarizer instruction ABOVE the
  transcript and serialised into every `/api/state` response. A tap-token
  holder — deliberately the LOWER-privilege credential — gets an
  instruction-position injection and a per-tick payload cost. Every
  operator-supplied text field goes through `validate_config_text`.
- **`tls.py:99`** — cert and key are written non-atomically and `_looks_valid`
  never checks the key, so a crash between the two writes leaves a mismatched
  pair that is reused on every later boot and **cannot self-heal**.
- **`install_picker.py:379`** — a wrong-shaped (but valid JSON)
  `.tapscribe-install.json` raises `AttributeError` out of `main`, so start.sh
  aborts and the Recorder never boots — and the picker's own message tells
  operators to hand-edit that file.
- **`setup_install.py:51`** — `_PICKER_FAMILIES` omits `moonshine` and
  `write_picker_state` replaces the file wholesale, so any /setup install
  silently resets an operator's Moonshine selection.
- **`session_paths.py:173`** — no length bound on a session id, so an
  over-`NAME_MAX` component raises a bare `OSError` (500) instead of the 404
  every other malformed id gets. Windows' MAX_PATH makes the threshold lower.
- **`batch_pipeline.py:116`** — `except Exception` misses `CancelledError`, so a
  cancelled pipeline leaves its record `state="running"` forever and a Bridge's
  meeting card spins indefinitely.
- **`transcribers/faster_whisper.py:154`** — the `except TypeError`
  version-compat fallback also wraps `list(segments_iter)`, so a decode-time
  TypeError silently re-transcodes the whole WAV with degraded settings.
- **`transcribers/_chunked.py:90`** — `chunk_s`/`overlap_s` are validated
  independently, so `TAPSCRIBE_PARAKEET_CHUNK_S=10` with the default overlap
  passes both bounds and then dies per-WAV inside `chunk_windows`.
- **`recorder.py:272`** — `JobTracker.update` silently drops typo'd field names
  via `hasattr`, while its sibling `ActiveStreams._apply` deliberately raises
  (and has a test pinning that). A typo'd `job.update()` freezes the job bar
  with no error.

---

## D. Bridges — reviewed, not applied

- **`content.js:896`** — the "don't reconnect after 4401" guard covers only the
  onclose ladder; the PCM path re-dials on the very next frame with no backoff
  (verified: 5 frames → 5 sockets, clock never advanced). The guard test passes
  only because it stops posting PCM after the close.
- **`content.js:371`** — `isTransportError` omits `tls-required`, so after 3 s
  of buffered speech the one actionable error is overwritten with
  `recorder-unreachable`, whose advice ("use the popup's Test connection") is a
  dead end: the popup runs on `chrome-extension://`, where the mixed-content
  check is inactive by design, so it reports everything fine.
- **`page-script.js:161`** — `tap()` keys on identity, not track, and nothing
  listens for `ended`. LiveKit's device switch swaps `mediaStreamTrack` in place
  with no event, so the bridge stays bound to a stopped track: byte counter
  climbs, pill stays green, **WAV records silence for the rest of the meeting**.
  The JS bridge has no equivalent of the #218 capture-failure signal.
- **`content.js:137`** — an End-meeting request arriving before the settings
  read resolves is dropped permanently (verified: 0 triggers, 60 s of virtual
  time changed nothing).
- **`tests/harness.js:62`** — swallows every exception thrown in a timer
  callback and in the storage listener; the comments promise surfacing that was
  never implemented, over exactly the least-asserted paths.
- **`bridges/README.md:84`** — the wire contract never documents the reserved
  `__probe__` identity, so a third-party bridge probing with its own id writes a
  roster occurrence that auto-binds a durable Person — one junk speaker per
  Test-connection click.

---

## E. Test-quality and coverage gaps — reviewed, not applied

- **`test_dashboard_ui.py:171`** — 65 tests swallow a Chromium LAUNCH failure
  into `pytest.skip`, so the only leg that runs any dashboard test can exit 0
  having exercised nothing. `harness.launch_bridge_context` deliberately does
  the opposite, and `test_setup_ui.py` in the same job has zero such swallows.
- **`test_dashboard_ui.py`** — selects on the exact presentational classes
  CLAUDE.md names as forbidden (`.wavrow__dur` at :3757, `.segctl__opt` at
  :4073), plus `.srcsw__opt`, `.cacherow__src`, `.panel__title`. A re-vendor of
  `web/vc/` turns the job red with no behaviour change.
- **`pyproject.toml:248`** — the package-data globs have no built-artifact
  test; the test the repo points at passes from a source checkout even with the
  whole block deleted, and the `test` job never builds the project. This class
  already shipped once, and #378 just added `vad/*.onnx` to the same block.
  Fix: `pip wheel --no-deps .` into tmp_path, assert the zip namelist.
- **Bundle Launcher has no executed test in CI** — `bundle-core-crossplatform`
  tests only Core; `bundle-build` is compile-only. #376's five verified Launcher
  runtime fixes shipped with no regression guard.
- **Parakeet real_audio E2E runs on no leg** — the only `-m real_audio` job
  lacks transformers. **This is the coverage gap that let the stitch
  duplication ship.**
- **`test_pipeline_resilience.py:373`** — the test named
  `..._and_recovers_for_next_tap` punts the recovery half in its body, so
  TapRelay's reconnect-with-backoff has no e2e guard while a green test name
  says it does. Its skip guard is also dead.
- **`test_dashboard_ui.py:3831`, :5073, :1995** — assertions gated on a fixed
  `wait_for_timeout` rather than the condition; :5073 and :1995 "cross a poll
  tick" without proving a poll fired, and under ADR-0013's idle backoff the
  window can contain zero polls. The sweep at :1736 shows the right pattern.
- **`test_wav_cache.py`** — no test anywhere passes a non-None `initial_prompt`,
  `hotwords` or `source_lang` to `cached_transcribe`, so the cache's central
  staleness guarantee is entirely unguarded (AST-scanned every call site).
- **`recorder.py:487`** — `UtteranceIndex.try_resume`'s cross-session guard is
  covered only by a test that is green EITHER WAY: removing the guard sends it
  down a `pytest.xfail` branch, which reports as expected-failure.
- **`test_tap_fan_out_chaos.py:136`** — `except RuntimeError: pass` with no
  why-comment (the only bare swallow left in the repo — all 32 others carry
  one). It also passes vacuously if `__aexit__` ever suppresses. Fix:
  `with pytest.raises(RuntimeError):`.
- **`test_transcribers_mlx_voxtral.py:176`** — the smoke can never execute:
  `mlx-voxtral` is in no extra and has no lane, so BOTH halves of CLAUDE.md's
  upstream-symbol rule are missing. It guards upstream's typo'd
  `apply_transcrition_request`.
- **`test_summarizers_local.py:422`** — the llama_cpp contract runs on no leg
  (a plain CPU wheel — its absence is an oversight, not a platform constraint)
  and only checks `hasattr`, not signatures.
- **`test_speech_gate.py:361`** — asserts `out is None or isinstance(out, dict)`,
  i.e. the declared return type, so it cannot fail for any implementation that
  honours its own annotation.
- **`model-select.test.js:72`** — tautological: `LIVE_FAMILY_LABELS` IS
  `FAMILY_LABELS.filter(...)`, so both sides are the same tuple references and
  the assertion would keep passing if every label were wrong.
- **`conftest.py:698` / `test_install_matrix.py:32`** — `read_text()` without
  `encoding="utf-8"` on pyproject/install-matrix. Green today on cp1252, but a
  `”` or `‐` added to any comment reddens all three Windows legs at once.
- **Missing guards named by the agents:** no test sweeps picker extras against
  pyproject's declared extras (which is how the phantom `[vad]` survived); no
  test pins `BRIDGE_ARTIFACTS.filename` against release.yml's asset names; no
  test for the cross-session transcript-search client state machine; the
  `max_speech`/`possible_ends` branch of the VAD port (its most intricate ~30
  lines) is never executed even locally.

---

## F. Architectural — recommend as follow-up issues, deliberately NOT applied

These are multi-file refactors that risk behaviour change; they would bury the
bug fixes above in noise.

- **`app.py` Divergent Change** — 2197 lines, 80 commits in 6 months, four
  independent axes (static assets, install wizard, Bridge control plane, People
  Registry) on top of the transcription API. `APIRouter` is used nowhere in the
  repo; splitting those four out would make a frontend-asset PR stop colliding
  with a transcription PR.
- **Summarizer `source` Repeated Switches** — the same three-way cascade at five
  hand-written sites across Python and JS; a fourth source silently mis-sends
  its body if any is missed, and `summary.js:270`'s negated branch is already
  wrong by construction for a source carrying neither model nor max_tokens. The
  Transcriber side solved this declaratively with `ModelEntry.inputs`.
- **`_build_state_blob` Data Clump** — 13 positional params, nine of them one
  Recorder snapshot; each new `/api/state` field costs four coordinated edits.
- **`SourceKind` Primitive Obsession** — `"original"|"stripped"` as a bare `str`
  in ~12 signatures, re-branched at four sites, only one of which rejects an
  unknown value. A non-route caller passing `"Stripped"` silently merges the
  ORIGINAL WAVs. Both sibling enums are already `Literal`.
- **`install_picker.FAMILIES` vs `catalog.ModelEntry.family`** — already
  divergent (`nb-whisper` is folded into the whisper row, undocumented). A new
  catalog family with no `FamilyDef` is uninstallable and invisible, with no
  failing test.
- **WAV-row cell fill duplicated** between `transcript.js:508` and
  `recordings.js:630`, already drifted (name truncation 30 vs 40).
- **`sessions.py:742`** — the poll caches every session's FULL parsed transcript
  for the process lifetime though only a 4-field projection is consumed;
  **`sessions.py:621`** — orphan attribution is O(originals × regions) per tick.
- **`session_merge.py:177`** — the from/to range filter runs AFTER the expensive
  size/duration/RMS gates, so a 3-WAV range against a 400-WAV meeting reads all
  400 WAVs end to end.
