// @ts-check
// Optimistic inline editing on /next, in two halves (#355):
//
//   · `createOverlay` — the PENDING EDIT: what the operator has typed but the
//     server hasn't confirmed, plus the per-tick catch-up sweep that retires an
//     entry once the server agrees.
//   · `createFieldSaver` — the SAVE: one debounced PUT per record id, narrated
//     through the shared save-status lifecycle (../save-status.js, which the
//     save BUTTONS use too).
//
// Both are generic over the resource. The domain owner that binds them lives
// elsewhere — next/session-labels.js for session labels, people.js for Person
// names — so adding an editable field means writing an owner, not another copy
// of this machine (three had already drifted when #355 landed).
//
// DOM-free: the caller supplies the PUT and a StatusTarget, so the whole
// lifecycle is unit-testable under node:test's mock.timers.

import { runSaveWithStatus } from "../save-status.js";

/** The window a burst of keystrokes coalesces into one PUT. */
export const SAVE_DEBOUNCE_MS = 600;

/**
 * A pending-edit overlay for one resource: id → the value as typed, until the
 * server catches up.
 * @template R
 * @typedef {object} OverlayOptions
 * @property {(record: R) => string} idOf The record's id, as the overlay keys it.
 * @property {(record: R) => string} baselineFor What the SERVER currently holds
 *   for this record, empty-string-normalized. A pending edit equal to the
 *   baseline has nothing left to save, so `sweep` retires it.
 */

/**
 * @template R
 * @typedef {object} OptimisticOverlay
 * @property {(id: string) => string | undefined} get The pending edit, if any.
 * @property {(id: string, value: string) => void} set Record what was typed.
 * @property {(id: string) => void} forget Drop a pending edit — which is also
 *   the ONLY way to cancel its save (see `createFieldSaver`).
 * @property {(records: readonly R[]) => void} sweep The catch-up sweep. Typed on
 *   the overlay's own record type, so sweeping the wrong collection (the classic
 *   copy-paste slip, and a silent one — no id would ever match, so nothing is
 *   ever retired) is a tsc error rather than a runtime no-op.
 * @property {(id: string) => void} claim Mark a save as owning this entry, so
 *   the sweep leaves it alone. For `createFieldSaver`; not a view-level verb.
 * @property {(id: string) => void} release Undo `claim`.
 */

/**
 * @template R
 * @param {OverlayOptions<R>} opts
 * @returns {OptimisticOverlay<R>}
 */
export function createOverlay({ idOf, baselineFor }) {
  /** @type {Map<string, string>} */
  const pending = new Map();
  /** Ids with a save scheduled or in flight — see `claim`. */
  /** @type {Set<string>} */
  const claimed = new Set();
  return {
    get: (id) => pending.get(id),
    set: (id, value) => void pending.set(id, value),
    forget: (id) => void pending.delete(id),
    claim: (id) => void claimed.add(id),
    release: (id) => void claimed.delete(id),

    /**
     * Drop every pending edit the server has caught up to — for EVERY record in
     * the tick, not just the focused one. An entry stranded on a record (edit,
     * then move on inside the debounce + poll window) would otherwise mask a
     * later change made elsewhere (another view, another tab) FOREVER: the sig
     * sites read the overlay first, so the server's new value could never even
     * trigger a rebuild, and refocusing would seed the input with the stale
     * value, inviting a save that reverts the external change. The debounce
     * window is safe: an entry the operator just typed differs from the baseline
     * until its PUT lands (so it survives), and when it EQUALS the baseline
     * there is nothing left to save.
     *
     * An id with a save SCHEDULED OR IN FLIGHT is skipped: "equals the baseline"
     * then can't be told apart from "equals a baseline this very save is about
     * to overwrite", and retiring it would silently drop the newer edit. The
     * canonical case is edit-then-revert — type "A" (its PUT starts), delete it
     * again, and a tick landing on a snapshot that still shows the ORIGINAL
     * value would retire the revert, letting "A" win a rename the operator
     * explicitly undid. The claim is released when the save settles, so the very
     * next tick retires the entry if the server really has caught up.
     * @param {readonly R[]} records
     */
    sweep(records) {
      for (const record of records) {
        const id = idOf(record);
        if (claimed.has(id)) continue;
        if (pending.get(id) === baselineFor(record)) pending.delete(id);
      }
    },
  };
}

/**
 * @typedef {object} FieldSaverOptions
 * @property {OptimisticOverlay<any>} overlay The pending edits this saver persists.
 *   Read AGAIN when the debounce timer fires, so a save no-ops if the entry is
 *   gone by then — which is what makes `overlay.forget(id)` (or a sweep) the one
 *   and only way to cancel a pending save, for every saver over this overlay.
 * @property {(id: string, value: string) => Promise<unknown>} put
 * @property {() => void} afterSave Run once a save settles, success or failure —
 *   the repaint kick (`ctx.afterMutate`), so the tick reflects the save at once
 *   instead of waiting out the poll backoff.
 */

/**
 * Build a saver over `overlay`: per-id debounced `put` of whatever the overlay
 * holds when the timer fires, narrated into the target the call site supplies.
 * @param {FieldSaverOptions} opts
 */
export function createFieldSaver({ overlay, put, afterSave }) {
  /** @type {Map<string, ReturnType<typeof setTimeout>>} */
  const timers = new Map();

  /** @param {string} id @param {import('../save-status.js').StatusTarget} target */
  const flush = async (id, target) => {
    timers.delete(id);
    const value = overlay.get(id);
    // Nothing left to save: the catch-up sweep retired the entry, or the record
    // was deleted. No PUT, and no badge for a save that never ran.
    if (value === undefined) {
      overlay.release(id);
      return;
    }
    try {
      await runSaveWithStatus(target, () => put(id, value), { afterSettle: afterSave });
    } finally {
      // Released only once the PUT has settled, so the sweep can't retire an
      // entry mid-flight (see `sweep`).
      overlay.release(id);
    }
  };

  return {
    /** Schedule a save for `id`, replacing any save already pending for it.
     * @param {string} id @param {import('../save-status.js').StatusTarget} target */
    save(id, target) {
      clearTimeout(timers.get(id));
      overlay.claim(id);
      timers.set(id, setTimeout(() => flush(id, target), SAVE_DEBOUNCE_MS));
    },
  };
}
