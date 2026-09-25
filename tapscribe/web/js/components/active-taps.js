// @ts-check
// gate-allow: signal-listener — handlers ride nodes this component builds; replaced subtrees take their listeners with them, and the few persistent targets are wired exactly once per page.
// Active taps panel — one row per live audio source the recorder is
// currently receiving bytes from.

import { tpl, mount, pick, deferIfSelectionInside } from "../templates.js";
import { speakerIndex } from "../speakers.js";
import { fmtBytes, fmtDur, truncMid } from "../formatters.js";
import { putJson, mutateButton, errText } from "../api.js";

// Per-host render state, keyed by bodyEl (NOT module scope — active-taps renders
// into several hosts at once on /next: the global rail + the Taps view, so a
// shared sentinel would cross-talk). Tracks the mounted mode (idle vs active)
// and, while active, a map of tap identity → its row element so we update rows
// IN PLACE instead of rebuilding every row via replaceChildren each tick.
//
// During a live meeting the old code re-created ~10-15 DOM nodes per tap, 1-2×/s
// indefinitely — collectable, but steady GC pressure + layout cost for the whole
// meeting. Now an existing row's mutable cells (level meter, lag, buffer text,
// byte/dur counters, toggle state) are rewritten in place; nodes are only
// created when a NEW tap appears and removed when one disappears.
/** @typedef {{ mode: "idle" | "active" | null, rows: Map<string, HTMLElement> }} TapHostState */
/** @type {WeakMap<Element, TapHostState>} */
const _state = new WeakMap();

/**
 * Write a tap's current values into an existing (or fresh) row scope. Only
 * touches text/attributes/classes — never creates child nodes — so calling it
 * each tick on a live row is allocation-free.
 * @param {ParentNode} scope the `.stream-row-wrap` (or its fragment)
 * @param {import('../types.js').ActiveStream} a
 */
function fillRow(scope, a) {
  const recOn = a.record !== false;
  const liveOn = a.live !== false;
  const ident = a.identity || "";
  const filename = a.filename || "";
  const dur = (a.bytes_received || 0) / 32000;

  const row = /** @type {HTMLElement} */ (scope.querySelector(".stream-row"));

  const marker = pick(scope, "spkMarker");
  marker.dataset.spk = String(speakerIndex(a.name || ident));

  pick(scope, "name").textContent = a.name || "<anon>";
  const identEl = pick(scope, "ident");
  identEl.title = filename;
  identEl.textContent = `${ident} · ${truncMid(filename, 30)}`;

  pick(scope, "size").textContent = fmtBytes(a.bytes_received || 0);
  pick(scope, "dur").textContent = `~${fmtDur(dur)}`;

  // Volume meter — peak amplitude of recent PCM, 0.0–1.0. Quiet (<5%) shows
  // muted gray; below ~1% snaps to zero width so a near-silent room doesn't show
  // a confusing sliver. green→amber→red comes from CSS via data-zone.
  const level = Math.max(0, Math.min(1, Number(a.level) || 0));
  const meter = pick(scope, "meter");
  const fill = pick(scope, "meterFill");
  const pct = level < 0.01 ? 0 : Math.round(level * 100);
  fill.style.width = pct + "%";
  let zone = "silent";
  if (level >= 0.85) zone = "clip";
  else if (level >= 0.6) zone = "hot";
  else if (level >= 0.05) zone = "ok";
  meter.dataset.zone = zone;
  meter.setAttribute("aria-label", `volume ${pct}% (${zone})`);

  // Per-tap lag from the relay. Hidden when live is off or not yet reported.
  const lagEl = pick(scope, "lag");
  const lag = typeof a.lag_s === "number" ? a.lag_s : null;
  if (lag === null || !liveOn) {
    lagEl.hidden = true;
  } else {
    lagEl.hidden = false;
    lagEl.textContent = `lag ${lag.toFixed(1)}s`;
    lagEl.classList.toggle("lag-warn", lag >= 0.5 && lag < 2);
    lagEl.classList.toggle("lag-bad", lag >= 2);
  }

  for (const btn of /** @type {NodeListOf<HTMLButtonElement>} */ (row.querySelectorAll(".tap-toggle"))) {
    const which = btn.dataset.toggle;
    const on = which === "record" ? recOn : liveOn;
    btn.dataset.identity = ident;
    btn.dataset.state = on ? "1" : "0";
    btn.classList.toggle("on", on);
  }

  // Three-state status line under each row: ⟳ <text> / ⟳ listening… / ⏸ quiet.
  // Hidden when LIVE is off or there's nothing meaningful to show.
  const bufRow = pick(scope, "bufferRow");
  const bufText = pick(scope, "bufferText");
  const bufIcon = pick(scope, "bufferIcon");
  const buf = (a.buffer_transcription || "").trim();
  const gateOpen = !!a.gate_open;
  let icon = "";
  let text = "";
  let cls = "";
  if (liveOn) {
    if (buf) { icon = "⟳"; text = buf; cls = "buf-active"; }
    else if (gateOpen) { icon = "⟳"; text = "listening…"; cls = "buf-listening"; }
    else { icon = "⏸"; text = "quiet"; cls = "buf-quiet"; }
  }
  if (text) {
    bufRow.hidden = false;
    bufIcon.textContent = icon;
    bufText.textContent = text;
    bufRow.className = "stream-buffer " + cls;
  } else {
    bufRow.hidden = true;
    bufText.textContent = "";
  }
}

/**
 * @param {import('../types.js').AppState} j
 * @param {import('../types.js').ActiveTapsCtx} ctx
 */
export function render(j, { countEl, badgeEl, bodyEl }) {
  const list = j.active || [];
  const count = String(list.length);
  if (countEl.textContent !== count) countEl.textContent = count;

  // Hold ALL row mutations while the operator is select-copying text inside
  // the panel (identity / name / filename are natural copy targets): fillRow
  // rewrites textContent unconditionally each tick, and assigning textContent
  // replaces the text node — dissolving a selection even when the value is
  // unchanged. Same interaction-state rule as renderRegion's guards; updates
  // resume on the first tick after the selection clears (deferIfSelectionInside
  // also marks the deferred-render flag, so main.js retries even if the poll
  // goes quiet — 304s — in between; issue #245).
  if (deferIfSelectionInside(bodyEl)) return;

  let st = _state.get(bodyEl);
  if (!st) {
    st = { mode: null, rows: new Map() };
    _state.set(bodyEl, st);
  }

  if (!list.length) {
    // Idle: mount the empty state ONCE (re-mounting every tick churned ~15
    // detached nodes/sec that the operator's tab accumulated until OOM).
    if (st.mode !== "idle") {
      mount(badgeEl, tpl("tpl-active-badge-idle"));
      mount(bodyEl, tpl("tpl-active-empty"));
      st.mode = "idle";
      st.rows.clear();
    }
    return;
  }

  // Entering the active state from idle/first-render: swap the badge + clear the
  // empty-state node once. The rows themselves are then managed in place below.
  if (st.mode !== "active") {
    mount(badgeEl, tpl("tpl-active-badge-capturing"));
    bodyEl.replaceChildren(); // static-render — one-shot clear on the idle→active transition; rows are then managed in place
    st.mode = "active";
    st.rows.clear();
  }

  const seen = new Set();
  /** @type {HTMLElement | null} */
  let prevRow = null;
  for (const a of list) {
    const id = a.identity || "";
    seen.add(id);
    let wrap = st.rows.get(id);
    if (!wrap) {
      wrap = /** @type {HTMLElement} */ (tpl("tpl-stream-row").firstElementChild);
      st.rows.set(id, wrap);
    }
    fillRow(wrap, a);
    // Keep list order WITHOUT re-appending every row each tick: moving an
    // attached node is a remove+insert even when it lands in the same slot,
    // which dirties layout (and restarts CSS transitions) K times per tick.
    // Only rows that are new or genuinely out of order get inserted.
    if (wrap.parentNode !== bodyEl || wrap.previousElementSibling !== prevRow) {
      bodyEl.insertBefore(wrap, prevRow ? prevRow.nextSibling : bodyEl.firstChild);
    }
    prevRow = wrap;
  }
  // Drop rows for taps that are gone.
  for (const [id, wrap] of st.rows) {
    if (!seen.has(id)) {
      wrap.remove();
      st.rows.delete(id);
    }
  }
}

/**
 * Compute the tap-settings PUT intent for a `.tap-toggle` button, or null
 * when the click should be ignored (disabled, or missing identity/kind).
 * Pure — no DOM writes — so the validation + next-value branching is
 * unit-testable without a DOM (node --test).
 * @param {{ disabled: boolean, dataset: { identity?: string, toggle?: string, state?: string } }} btn
 * @returns {{ identity: string, which: string, next: boolean } | null}
 */
export function toggleIntent(btn) {
  if (btn.disabled) return null;
  const identity = btn.dataset.identity;
  const which = btn.dataset.toggle;
  if (!identity || !which) return null;
  return { identity, which, next: btn.dataset.state !== "1" };
}

/**
 * Wire the delegated rec/live toggle click handler onto a host that renders
 * rows via render() above. Bind ONCE per host — render() keys its row state
 * by `bodyEl` because active-taps mounts into several hosts at once (the
 * global rail + the Taps view), and a delegated listener on the parent
 * survives every per-tick row swap, so this only needs binding once too.
 * Flips the visual state immediately so the click feels responsive; the next
 * poll repaints from the authoritative state.
 * @param {HTMLElement} bodyEl
 * @param {{ afterMutate: () => void }} ctx
 */
export function wireToggles(bodyEl, { afterMutate }) {
  bodyEl.addEventListener("click", (ev) => {
    const btn = /** @type {HTMLButtonElement | null} */ (
      /** @type {Element | null} */ (ev.target)?.closest(".tap-toggle"));
    if (!btn) return;
    const intent = toggleIntent(btn);
    if (!intent) return;
    const { identity, which, next } = intent;
    btn.dataset.state = next ? "1" : "0";
    btn.classList.toggle("on", next);
    mutateButton(btn, () => putJson("/api/tap-settings", { identity, [which]: next }), {
      afterMutate,
      failMessage: (e) => `Tap setting toggle failed: ${errText(e)}`,
    });
  });
}
