// @ts-check
// Stages-only shared helpers. New file under js/next/ so the existing shared
// modules stay untouched — see the Phase-1 brief. Mostly pure DOM + template
// glue, plus a couple of shared click-to-mutate wirings (wireRecPill) that
// don't warrant their own module — AND THE owner of "which audio of a session
// the dashboard acts on", the source pick both stages read and write (#354; see
// CONTEXT.md "Source pick · original / stripped · effective source").

import { tpl, pick } from "../templates.js";
import { postJson, mutateButton } from "../api.js";
import { createProgressSync } from "../vc/components/progress/progress.js";
import { createEmptyStateSync } from "../vc/components/empty-state/empty-state.js";
import { serverSessionLabel } from "./session-labels.js";

/** The Stages views. GLOBAL group is pinned + un-numbered; the THIS SESSION
 * group is the numbered Capture → Recordings → Transcript → Summary journey. */
/** @typedef {"capture"|"transcript"|"summary"|"settings"|"taps"|"recordings"|"people"|"sessions"} ViewId */

/**
 * One source of truth for every Stages view's metadata: the spine's groups and
 * labels, main.js's template list, its module lookup and its cache keys all
 * derive from this. A new view still needs its own `views/<id>.js` and a
 * `buildChip` case in spine.js — those are code, not metadata.
 *
 * @type {Map<ViewId, ViewEntry>}
 * @typedef {{
 *   group: "global" | "journey",
 *   name: string,
 *   lead: string,
 *   template: string,
 *   sessionKey?: boolean,
 * }} ViewEntry
 */
export const VIEWS = new Map([
  ["taps",      { group: "global",  name: "Taps",      lead: "🛰️", template: "/web/components/next/taps.html" }],
  // Sessions: the scannable all-sessions list — the spine's <select> doesn't scale.
  ["sessions",  { group: "global",  name: "Sessions",  lead: "🗂️", template: "/web/components/next/sessions.html" }],
  ["people",    { group: "global",  name: "People",    lead: "👥", template: "/web/components/next/people.html" }],
  ["settings",  { group: "global",  name: "Settings",  lead: "⚙️", template: "/web/components/next/views.html" }],
  ["capture",   { group: "journey", name: "Capture",   lead: "1",  template: "/web/components/next/views.html" }],
  ["recordings",{ group: "journey", name: "Recordings",lead: "2",  template: "/web/components/next/recordings.html" }],
  ["transcript",{ group: "journey", name: "Transcript",lead: "3",  template: "/web/components/next/views.html", sessionKey: true }],
  ["summary",   { group: "journey", name: "Summary",   lead: "4",  template: "/web/components/next/summary.html" }],
]);

/** True for a `viewCache` key belonging to a session-keyed view. Those keys are the only
 * unbounded ones (one per visited session), and main.js prunes and refreshes them by this
 * predicate rather than by a literal prefix, so `sessionKey` stays the single source.
 * @param {string} key */
export function isSessionKeyedCacheKey(key) {
  return [...VIEWS].some(([id, e]) => e.sessionKey && key.startsWith(`${id}:`));
}

/** The ids in one spine group, in VIEWS order.
 * @param {ViewEntry["group"]} group @returns {ViewId[]} */
const inGroup = (group) => [...VIEWS.entries()].filter(([, e]) => e.group === group).map(([id]) => id);
export const GLOBAL_VIEWS = inGroup("global");
export const JOURNEY_VIEWS = inGroup("journey");
/** @type {ViewId[]} */
export const ALL_VIEWS = [...VIEWS.keys()];

/**
 * Build the cache key for a view instance. Per-session for views that carry
 * session-specific state (transcript merged pane); page-singleton for others.
 * @param {ViewId} view
 * @param {import('../types.js').Session | null} session
 * @returns {string}
 */
export function viewKey(view, session) {
  const entry = VIEWS.get(view);
  if (entry?.sessionKey) return `${view}:${session?.session || ""}`;
  return view;
}

/** Last-rendered signature per header host. The per-tick views (Capture,
 * Taps, Sessions, People) call header() on every poll; without a gate each
 * call cloned the template and replaceChildren'd even when nothing changed —
 * ~10 collectable nodes + a layout pass per view per tick, the same churn
 * class the idle-OOM fix killed elsewhere. Renders with `actions` are never
 * gated (the Node carries fresh listeners a string signature can't capture)
 * and clear the stored sig so a following gated render can't skip falsely. */
/** @type {WeakMap<Element, string>} */
const _headerSig = new WeakMap();

/**
 * The per-tick header rebuild gate, factored out of header() so it can be
 * unit-tested without a DOM (node --test has no browser; `host` is only a
 * WeakMap key). Returns true when a rebuild is needed — the sig changed, or
 * `hasActions` (a fresh-listener Node no string sig can capture, always
 * rebuilt) — and records the new sig. Actions clear the stored sig so a later
 * gated render with the same sig can't skip falsely.
 * @param {Element} host
 * @param {string} sig
 * @param {boolean} hasActions
 */
export function headerNeedsRender(host, sig, hasActions) {
  if (hasActions) {
    _headerSig.delete(host);
    return true;
  }
  if (_headerSig.get(host) === sig) return false;
  _headerSig.set(host, sig);
  return true;
}

/**
 * Build the shared stage header into a view's `[data-slot=head]` host.
 * `sub`/`actions` accept either a string (→ textContent) or a Node to append,
 * so callers never inject HTML strings (XSS-safe, like the rest of the code).
 * `sub` may also be a LAZY `{ sig, build }` pair: per-tick views build an
 * `inline(...)`/`strong(...)` fragment via `build()`, invoked ONLY past the gate
 * and keyed on `sig`, so the throwaway allocation is skipped on unchanged ticks
 * (#246). Pairing `sig` WITH the builder makes a forgotten sig a type error, not
 * a silent stale header. Skips the rebuild when eyebrow/title/sub key are
 * unchanged (see above).
 * @param {Element} host
 * @param {{ eyebrow: string, title: string, sub?: string | Node | { sig: string, build: () => string | Node }, actions?: Node }} opts
 */
export function header(host, { eyebrow, title, sub, actions }) {
  // Detect the lazy pair by duck-typing `build` rather than `!(sub instanceof
  // Node)` — the latter touches the DOM-only `Node` global before the gate can
  // skip, which breaks the headless gate test (and needlessly on a skip tick).
  const lazy = typeof sub === "object" && sub !== null && "build" in sub;
  const subKey = lazy ? sub.sig : sub instanceof Node ? sub.textContent || "" : sub ?? "";
  if (!headerNeedsRender(host, `${eyebrow}§${title}§${subKey}`, !!actions)) return;
  const resolved = lazy ? sub.build() : sub; // build() runs ONLY on a real rebuild
  const frag = tpl("tpl-next-head");
  pick(frag, "eyebrow").textContent = eyebrow;
  pick(frag, "title").textContent = title;
  const subEl = pick(frag, "sub");
  if (resolved instanceof Node) subEl.appendChild(resolved);
  else if (resolved != null) subEl.textContent = resolved;
  if (actions) pick(frag, "actions").appendChild(actions);
  host.replaceChildren(frag);
}

/**
 * A tiny inline <b>text</b> span — used to emphasise a value inside an
 * otherwise plain header sub-line without resorting to innerHTML.
 * @param {string} text
 */
export function strong(text) {
  const b = document.createElement("b");
  b.textContent = text;
  return b;
}

/**
 * Compose several inline nodes/strings into one fragment for a header sub.
 * @param {...(string | Node)} parts
 */
export function inline(...parts) {
  const frag = document.createDocumentFragment();
  for (const p of parts) frag.append(p);
  return frag;
}

/** Job-kind → the label the shared progress bar shows. One source of truth for
 * the Stages views that render the per-session job (Transcript, Recordings,
 * Summary) — adding a job kind is a one-line edit here, not a sweep across
 * every view. Mirrors the backend `JobState.kind` literal. */
export const JOB_LABELS = {
  transcribe: "Transcribing",
  strip: "Stripping silence",
  summarize: "Summarizing",
  pipeline: "Pipeline",
  delete: "Deleting audio",
};

/**
 * Render the shared job-progress bar from a JobState snapshot, mutating the
 * prebuilt nodes IN PLACE — never rebuilding DOM. The bar ticks ~1/s during a
 * job and MUST stay outside the per-tick signature gates: sharing a signature
 * with an O(content) region (the merged transcript) was the "/next freezes
 * while transcribing" bug (see CLAUDE.md render-signature hygiene). Hidden when
 * there's no job, or — when `only` is given — when the running job isn't that
 * kind (so the Summary panel shows a summarize job but not a transcribe/strip
 * on the same session).
 * @param {{ jobBar: HTMLElement, jobLabel: HTMLElement, jobCount: HTMLElement, jobProgress: HTMLElement, jobWav: HTMLElement }} hosts
 * @param {import('../types.js').JobStateSnapshot | null} job
 * @param {{ only?: import('../types.js').JobStateSnapshot["kind"] }} [opts]
 */
export function renderJobBar({ jobBar, jobLabel, jobCount, jobProgress, jobWav }, job, { only } = {}) {
  // A pipeline job in stage X counts as an X job for the `only` filter, so
  // e.g. the Summary panel's bar shows the pipeline's summarize stage.
  const effectiveKind = job?.kind === "pipeline" && job.stage ? job.stage : job?.kind;
  if (!job || (only && effectiveKind !== only)) {
    jobBar.hidden = true;
    return;
  }
  jobBar.hidden = false;
  jobLabel.textContent =
    job.kind === "pipeline" && job.stage
      ? `${JOB_LABELS.pipeline} · ${JOB_LABELS[job.stage] || job.stage}`
      : JOB_LABELS[job.kind] || "Working";
  jobCount.textContent = `${job.current} / ${job.total}`;
  // vc progress, one instance per host, mounted lazily on the first visible
  // tick (warmProgress() runs at boot in main.js, so the sync build is safe)
  // and mutated in place via setValue afterwards — same "never rebuild on a
  // job tick" rule as the rest of this bar.
  let meter = _jobMeters.get(jobProgress);
  if (!meter) {
    meter = createProgressSync({ value: job.current, max: job.total });
    jobProgress.replaceChildren(meter.el); // static-render — one-shot mount of the meter shell
    _jobMeters.set(jobProgress, meter);
  } else {
    meter.setValue(job.current, job.total);
  }
  jobWav.textContent = job.current_file ? `current: ${job.current_file}` : "";
}

/** One vc progress instance per jobProgress host. @type {WeakMap<Element, { el: HTMLElement, setValue: (v: number, m?: number) => void }>} */
const _jobMeters = new WeakMap();

/** The session-scoped source pick store: session id → "original" | "stripped".
 * Shared by the Recordings and Transcript views so a pick in one view is the
 * pick the other view reads and acts on. In-memory only — not persisted. */
/** @type {Map<string, "original" | "stripped">} */
const _sourcePick = new Map();

/**
 * Record the operator's source pick for one session.
 * @param {string} sessionId
 * @param {"original" | "stripped"} source
 */
export function setSourcePick(sessionId, source) {
  _sourcePick.set(sessionId, source);
}

/**
 * Clear a session's source pick, returning it to the default (original).
 * @param {string} sessionId
 */
export function clearSourcePick(sessionId) {
  _sourcePick.delete(sessionId);
}

/**
 * Build the original/stripped source toggle (template `tpl-next-srcsw`), shared
 * by the Recordings and Transcript views. The "stripped" button is disabled
 * until the session has a stripped/ folder; `onPick` fires with the chosen
 * source on a click of an enabled button, where the caller updates the shared
 * store via `setSourcePick` and invalidates its render signature.
 * @param {{
 *   active: "original" | "stripped",
 *   hasStripped: boolean,
 *   onPick: (which: "original" | "stripped") => void,
 * }} opts
 */
export function buildSourceToggle({ active, hasStripped, onPick }) {
  const sw = tpl("tpl-next-srcsw");
  for (const b of /** @type {NodeListOf<HTMLButtonElement>} */ (sw.querySelectorAll("[data-src]"))) {
    const which = /** @type {"original"|"stripped"} */ (b.dataset.src);
    if (which === active) b.classList.add("is-on");
    if (which === "stripped" && !hasStripped) {
      b.disabled = true;
      b.title = "no stripped/ folder — strip silence in Recordings first";
    }
    b.addEventListener("click", () => {
      if (b.disabled) return;
      onPick(which);
    });
  }
  return sw;
}

/**
 * The effective audio source for a session: the operator's pick from the
 * shared store, falling back to "original" when unpicked or when "stripped"
 * is picked but the session has no stripped/ folder (so a stale pick can't
 * operate on nothing after the clips were cleared).
 * @param {import('../types.js').Session | null} session
 * @returns {"original" | "stripped"}
 */
export function effectiveSource(session) {
  const want = _sourcePick.get(session?.session || "") || "original";
  return want === "stripped" && !session?.stripped ? "original" : want;
}

/**
 * Build the "Coming in a later phase" placeholder for a stubbed view.
 * @param {Element} root
 * @param {{ eyebrow: string, title: string, sub?: string, icon: string, heading: string, detail: string }} opts
 */
export function placeholderView(root, { eyebrow, title, sub, icon, heading, detail }) {
  const headHost = document.createElement("div");
  // vc empty-state (js/vc) — warmed at boot in main.js, so the sync build is
  // safe here. The reference leaf-atom adoption for view-level placeholders.
  const body = createEmptyStateSync({ icon, title: heading, detail }).el;
  root.replaceChildren(headHost, body);
  header(headHost, { eyebrow, title, sub });
}

/**
 * The recording-enabled pill's toggle target: the opposite of the current
 * effective state (armed by default when unset, so only an explicit `false`
 * targets `true`). Pure — factored out of wireRecPill so the branching is
 * unit-testable without a DOM.
 * @param {import('../types.js').AppState | null} state
 */
export function nextRecordingEnabled(state) {
  return state?.recording_enabled === false;
}

/**
 * Wire the recording-enabled pill (● recording / ⏸ paused), shared by the
 * Capture and Taps views (same contract in both: POST
 * /api/recording/toggle, then afterMutate()). `getState` reads the calling
 * view's latest poll snapshot at click time, via a closure over its own
 * `latest` variable, so the toggle always flips from the current value.
 * @param {HTMLButtonElement} btn
 * @param {() => import('../types.js').AppState | null} getState
 * @param {{ afterMutate: () => void }} ctx
 */
export function wireRecPill(btn, getState, { afterMutate }) {
  btn.addEventListener("click", () => {
    const enabled = nextRecordingEnabled(getState());
    mutateButton(btn, () => postJson("/api/recording/toggle", { enabled }), {
      afterMutate,
      failMessage: (e) => `Recording toggle failed: ${e}`,
    });
  });
}

/**
 * Paint the recording-enabled pill's label + state classes, shared by the
 * Capture and Taps views (both compute `enabled` the same way:
 * `j.recording_enabled !== false`).
 * @param {HTMLButtonElement} btn
 * @param {boolean} enabled
 */
export function paintRecPill(btn, enabled) {
  btn.textContent = enabled ? "● recording" : "⏸ paused";
  btn.classList.toggle("is-on", enabled);
  btn.classList.toggle("is-paused", !enabled);
}

/**
 * Resolve the session the Stages UI is focused on: the explicitly-selected
 * one, else the current (recording) session, else the newest on disk.
 * @param {import('../types.js').Session[]} sessions
 * @param {string | null} selectedId
 * @returns {import('../types.js').Session | null}
 */
export function resolveSession(sessions, selectedId) {
  if (!sessions.length) return null;
  if (selectedId) {
    const hit = sessions.find((s) => s.session === selectedId);
    if (hit) return hit;
  }
  const current = sessions.find((s) => s.is_current);
  return current ?? sessions[0] ?? null;
}

/**
 * Newest-first session comparator — ids are ISO-ish timestamps, so a
 * descending string sort is chronological. Shared by the spine's session
 * picker and the Sessions listing so their ordering can't drift.
 * @param {import('../types.js').Session} a
 * @param {import('../types.js').Session} b
 */
export function newestFirst(a, b) {
  return a.session < b.session ? 1 : -1;
}

/**
 * A session's display label: its server-side meta label, falling back to the
 * raw id. The label read routes through `serverSessionLabel`, which owns the
 * metaFor-equivalence rationale — so views needn't thread main.js's metaFor
 * (and this module stays importable side-effect-free under node --test). Lives
 * here, not main.js: a view importing main.js would execute its boot code.
 * Pending RENAMES are not consulted; a caller showing a label the operator may
 * be mid-edit on wants `sessionLabelFor` from next/session-labels.js.
 * @param {import('../types.js').Session} sess
 */
export function sessionLabel(sess) {
  return serverSessionLabel(sess) || sess.session;
}
