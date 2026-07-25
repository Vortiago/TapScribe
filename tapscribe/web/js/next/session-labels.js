// @ts-check
// THE owner of "a session's label as the operator is editing it" (#355).
//
// Two places rename a session — the Sessions list's inline row field
// (views/sessions.js) and the spine's Session Information card
// (components/spine.js) — through the same `PUT /api/session-meta/{sid}`
// `{label}`. Before this module each held its own optimistic overlay, its own
// debounce timers and its own copy of the catch-up sweep, so the two could
// disagree about the same session's pending rename; the v1.0.0 release review
// had to fix the same sweep bug twice. Everything about a pending rename now
// lives here: the overlay, ONE debounced saver (so the two editors share a
// single pending save per session rather than racing two timers), the
// label-resolution rule, and the per-tick sweep. Views import verbs, never the
// Map.
//
// The generic debounce/status machine underneath is next/field-saver.js.

import { putJson } from "../api.js";
import { statusTarget } from "../save-status.js";
import { createFieldSaver, createOverlay } from "./field-saver.js";

/** The pending renames: sid → the label as typed, until the server catches up.
 * Module-level, because a pending rename belongs to the SESSION, not to
 * whichever view happens to be mounted. */
const pending = createOverlay({
  idOf: (/** @type {import('../types.js').Session} */ s) => s.session,
  baselineFor: (s) => serverSessionLabel(s),
});

/** The repaint kick, supplied once by main.js (which owns the poll tick) so a
 * settled rename reflects immediately instead of waiting out the poll backoff
 * — the same `afterMutate` every view build gets. Defaults to a no-op so this
 * module stays importable, and unit-testable, without main.js's boot. */
let repaint = () => {};

/** Wire the repaint kick. Called once from main.js's boot.
 * @param {() => void} fn */
export function setSessionLabelRepaint(fn) {
  repaint = fn;
}

const saver = createFieldSaver({
  overlay: pending,
  put: (sid, label) => putJson(`/api/session-meta/${encodeURIComponent(sid)}`, { label }),
  // Indirect through `repaint` so main.js can wire it after this module loads.
  afterSave: () => repaint(),
});

/**
 * A session's label as the SERVER has it, empty-string-normalized. This is THE
 * one owner of the metaFor-equivalence rationale the sig sites lean on:
 * main.js's `metaFor(s).label` derives from `session_meta.label` alone, so
 * reading the raw field here is value-identical to what the render paints via
 * metaFor (+ the same overlay) while allocating no throwaway EffectiveMeta
 * (alias-map spread) per session per tick. It MUST stay value-identical — if
 * metaFor's label resolution ever changes (trim, a People-name fallback, an
 * auto-label), this helper is the single place the sig side follows, or every
 * sig-gated region using it goes stale after a rename (CLAUDE.md
 * render-signature hygiene). Render paths keep using metaFor/labelFor; sig,
 * filter, and overlay comparisons come through here.
 * @param {import('../types.js').Session} s
 */
export function serverSessionLabel(s) {
  return s.session_meta?.label || "";
}

/** The label to SHOW for a session: a pending rename if there is one, else the
 * server's. Used by the per-tick sig/filter paths.
 * @param {import('../types.js').Session} s */
export function sessionLabelFor(s) {
  return pending.get(s.session) ?? serverSessionLabel(s);
}

/** The pending rename for `sid`, or undefined. For render paths that fall back
 * to something other than the server label (a placeholder, an id).
 * @param {string} sid */
export function pendingSessionLabel(sid) {
  return pending.get(sid);
}

/** The pending rename for `sid` if there is one, else `serverValue` — the ONE
 * spelling of the overlay-first rule, for render paths that already hold the
 * server's label (main.js's `metaFor(s).label`) and so can't use
 * `sessionLabelFor`. Both must stay value-identical; see `serverSessionLabel`.
 * @param {string} sid @param {string} serverValue */
export function pendingOr(sid, serverValue) {
  return pending.get(sid) ?? serverValue;
}

/** EVERY status cell reporting a rename of `sid`, live at call time. Both editors
 * stamp their cell with `data-status-sid`, so ONE saver narrates into whichever
 * of them is currently mounted — the Sessions row AND the spine card. Resolved
 * per write, never captured: a cell captured when the row/card was built is
 * routinely DETACHED by the time the save settles (a rebuild, or the row leaving
 * a filtered list), and a `failed: …` written there is invisible.
 * @param {string} sid */
const statusCellsFor = (sid) =>
  document.querySelectorAll(`[data-status-sid="${CSS.escape(sid)}"]`);

/** Record what the operator just typed, and schedule the save. The status cells
 * are this module's business, not the caller's — see `statusCellsFor`.
 * @param {string} sid @param {string} label */
export function editSessionLabel(sid, label) {
  pending.set(sid, label);
  saver.save(sid, statusTarget(() => statusCellsFor(sid)));
}

/** Forget a pending rename for a session that's gone (deleted, absorbed). The
 * saver re-reads the overlay when its timer fires, so dropping the entry IS the
 * cancellation — for every editor, not just the one that called this — and a
 * queued PUT can't 404 against a folder that no longer exists.
 * @param {string} sid */
export function forgetSessionLabel(sid) {
  pending.forget(sid);
}

/**
 * Drop every pending rename the server has caught up to — the overlay's own
 * catch-up sweep (its doc carries the why: a stranded entry masks a later rename
 * made elsewhere forever).
 *
 * Called ONCE per tick by main.js's renderAll, before anything computes a label
 * sig — not per view. Two views used to sweep, which left the invariant living
 * implicitly in their call order (ADR-0004: "the hold lives at shared seams, not
 * per view").
 * @param {readonly import('../types.js').Session[]} sessions
 */
export function dropCaughtUpSessionLabels(sessions) {
  pending.sweep(sessions);
}
