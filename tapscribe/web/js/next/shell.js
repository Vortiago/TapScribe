// @ts-check
// Stages-only shared helpers. New file under js/next/ so the existing shared
// modules stay untouched — see the Phase-1 brief. Pure DOM + template glue.

import { tpl, pick } from "../templates.js";

/** The Stages views. GLOBAL group is pinned + un-numbered; the THIS SESSION
 * group is the numbered Capture → Recordings → Transcript → Summary journey. */
/** @typedef {"capture"|"transcript"|"summary"|"settings"|"taps"|"recordings"|"people"|"sessions"} ViewId */

/** @type {ViewId[]} */
export const GLOBAL_VIEWS = ["taps", "sessions", "people", "settings"];
/** @type {ViewId[]} */
export const JOURNEY_VIEWS = ["capture", "recordings", "transcript", "summary"];
/** @type {ViewId[]} */
export const ALL_VIEWS = [...GLOBAL_VIEWS, ...JOURNEY_VIEWS];

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
 * Build the shared stage header into a view's `[data-slot=head]` host.
 * `sub`/`actions` accept either a string (→ textContent) or a Node to append,
 * so callers never inject HTML strings (XSS-safe, like the rest of the code).
 * Skips the rebuild when eyebrow/title/sub text are unchanged (see above).
 * @param {Element} host
 * @param {{ eyebrow: string, title: string, sub?: string | Node, actions?: Node }} opts
 */
export function header(host, { eyebrow, title, sub, actions }) {
  const subText = sub instanceof Node ? sub.textContent || "" : sub ?? "";
  if (!actions) {
    const sig = `${eyebrow}§${title}§${subText}`;
    if (_headerSig.get(host) === sig) return;
    _headerSig.set(host, sig);
  } else {
    _headerSig.delete(host);
  }
  const frag = tpl("tpl-next-head");
  pick(frag, "eyebrow").textContent = eyebrow;
  pick(frag, "title").textContent = title;
  const subEl = pick(frag, "sub");
  if (sub instanceof Node) subEl.appendChild(sub);
  else if (sub != null) subEl.textContent = sub;
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
 * @param {{ jobBar: HTMLElement, jobLabel: HTMLElement, jobCount: HTMLElement, jobFill: HTMLElement, jobWav: HTMLElement }} hosts
 * @param {import('../types.js').JobStateSnapshot | null} job
 * @param {{ only?: import('../types.js').JobStateSnapshot["kind"] }} [opts]
 */
export function renderJobBar({ jobBar, jobLabel, jobCount, jobFill, jobWav }, job, { only } = {}) {
  if (!job || (only && job.kind !== only)) {
    jobBar.hidden = true;
    return;
  }
  jobBar.hidden = false;
  const pct = job.total > 0 ? Math.round((100 * job.current) / job.total) : 0;
  jobLabel.textContent = JOB_LABELS[job.kind] || "Working";
  jobCount.textContent = `${job.current} / ${job.total}`;
  jobFill.style.width = `${pct}%`;
  jobWav.textContent = job.current_file ? `current: ${job.current_file}` : "";
}

/**
 * Build the original/stripped source toggle (template `tpl-next-srcsw`), shared
 * by the Recordings and Transcript views. The "stripped" button is disabled
 * until the session has a stripped/ folder; `onPick` fires with the chosen
 * source on a click of an enabled button, where the caller updates its own
 * sourcePick map and invalidates its render signature.
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
 * Build the "Coming in a later phase" placeholder for a stubbed view.
 * @param {Element} root
 * @param {{ eyebrow: string, title: string, sub?: string, icon: string, heading: string, detail: string }} opts
 */
export function placeholderView(root, { eyebrow, title, sub, icon, heading, detail }) {
  const headHost = document.createElement("div");
  const body = tpl("tpl-next-placeholder");
  pick(body, "icon").textContent = icon;
  pick(body, "heading").textContent = heading;
  pick(body, "detail").textContent = detail;
  root.replaceChildren(headHost, body);
  header(headHost, { eyebrow, title, sub });
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
