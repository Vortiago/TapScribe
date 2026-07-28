// @ts-check
// The lazily-fetched per-session WAV listing, as the two views that read it
// need it: the Recordings WAV list and the Transcript per-WAV picker.
//
// `api.js`'s `sessionFiles` resource is the fetch + cache + failure-policy +
// stale-while-revalidate layer, and its `resolve` owns the per-tick choreography
// (lazy-resource.js). What is left here — and it is all both views were
// re-deriving identically — is the shape of the ANSWER: the cold sentinel must
// never be handed back as a value a caller could iterate, and "which of the four
// states is this region in" has a precedence that a view getting it wrong turns
// into a wrong answer rather than a slow one.
//
// DOM-free on purpose: no host, no placeholder, no row builder. The seam that
// paints rows is `renderList` (templates.js) and the placeholder WORDING is
// per-view content, not shared behaviour — folding either in here would have
// bought a ten-parameter factory to hide six lines, which is the shallow shape
// this module exists to avoid. Unit-tested under `node --test` with a fake
// resource, like field-saver.js and save-status.js.

import { sessionFiles } from "../api.js";

/**
 * @typedef {"none" | "loading" | "rows" | "empty"} ListState
 *
 * - `none`    — no session is focused; nothing to show.
 * - `loading` — a COLD fetch is in flight (this session has no last-good
 *               listing yet). A session that HAS one never reaches this state:
 *               it holds the previous rows while the new sig refetches (#266).
 * - `rows`    — there are rows to reconcile.
 * - `empty`   — the session resolved to no files.
 */

/**
 * Which of the four states a listing region is in. Pure, and the ONE place the
 * precedence lives: no-session beats loading, loading beats emptiness. Both
 * views used to inline this ternary, and a view that gets the order wrong shows
 * "no recordings yet" during a cold load — a wrong answer, not a slow one.
 * @param {{ hasSession: boolean, loading: boolean, count: number }} p
 * @returns {ListState}
 */
export function listState({ hasSession, loading, count }) {
  if (!hasSession) return "none";
  if (loading) return "loading";
  return count ? "rows" : "empty";
}

/**
 * One view's handle on the session WAV listing.
 *
 * `onLoaded` runs after a fetch SUCCEEDS — the view drops its render gates and
 * repaints there. It is bound ONCE here (`watch`), and is not called for a cache
 * hit (nothing changed) nor for a failed fetch (the stale hold stays up and the
 * poll paces the retry) — both `sessionFiles`' declared policy.
 *
 * @param {{
 *   onLoaded: () => void,
 *   source?: Pick<typeof sessionFiles, "watch">,
 * }} ctx — `source` is injectable so the answer shape is testable without api.js.
 */
export function createFilesSource({ onLoaded, source = sessionFiles }) {
  const listing = source.watch(onLoaded);
  return {
    /**
     * Resolve the focused session's listing for this tick.
     * @param {string} session — "" when no session is focused
     * @param {string} filesSig — the session's aggregate files stamp
     * @returns {{ files: import('../types.js').WavFile[], loading: boolean, stale: boolean }}
     *   `files` is always an array — the cold sentinel is reported as
     *   `loading`, never handed back as a value a caller could iterate.
     *   `stale` says the rows are the PREVIOUS sig's, held while this one
     *   refetches: both WAV lists gate on a signature containing `files_sig`, so
     *   without it in the gate the swap from held rows to this sig's own rows
     *   carries no signature change and is skipped (leaving rows that predate a
     *   failed-then-retried fetch on screen).
     */
    resolve(session, filesSig) {
      const { value, loading, stale } = listing.resolve([session, filesSig]);
      return { files: value || [], loading, stale };
    },
  };
}
