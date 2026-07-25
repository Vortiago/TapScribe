// @ts-check
// The lazily-fetched per-session WAV listing, as the two views that read it
// need it: the Recordings WAV list and the Transcript per-WAV picker.
//
// `api.js`'s `loadSessionFiles` is the fetch + cache + stale-while-revalidate
// layer. What both views were re-deriving ON TOP of it, identically, is the
// small bit around it: an in-flight key set per view, the `null`-means-COLD
// sentinel, and the four states a listing region can be in. Neither view needs
// to know that `null` is the cold sentinel and `[]` is "nothing to fetch" — that
// distinction only exists to answer "which of the four states am I in", so it
// belongs here rather than being spelled out at each call site.
//
// DOM-free on purpose: no host, no placeholder, no row builder. The seam that
// paints rows is `renderList` (templates.js) and the placeholder WORDING is
// per-view content, not shared behaviour — folding either in here would have
// bought a ten-parameter factory to hide six lines, which is the shallow shape
// this module exists to avoid. Unit-tested under `node --test` with a fake
// loader, like field-saver.js and save-status.js.

import { loadSessionFiles } from "../api.js";

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
 * repaints there. It is not called for a cache hit (nothing changed) nor for a
 * failed fetch (the stale hold stays up and the poll paces the retry), both of
 * which are `loadSessionFiles`' contract, kept intact here.
 *
 * @param {{
 *   onLoaded: () => void,
 *   load?: (session: string, filesSig: string, pending: Set<string>, onLand: () => void)
 *            => import('../types.js').WavFile[] | null,
 * }} ctx — `load` is injectable so the resolve logic is testable without api.js.
 */
export function createFilesSource({ onLoaded, load = loadSessionFiles }) {
  /** (session@files_sig) fetches in flight — dedupes across the ticks before
   * one lands (the api.js cache dedupes the REQUEST itself; this dedupes the
   * per-view bookkeeping around it). Per source, so two views watching the same
   * session don't share a set and cancel each other's first fetch. */
  const pending = new Set();

  return {
    /**
     * Resolve the focused session's listing for this tick.
     * @param {string} session — "" when no session is focused
     * @param {string} filesSig — the session's aggregate files stamp
     * @returns {{ files: import('../types.js').WavFile[], loading: boolean }}
     *   `files` is always an array — the cold sentinel is reported as
     *   `loading`, never handed back as a value a caller could iterate.
     */
    resolve(session, filesSig) {
      const fetched = load(session, filesSig, pending, onLoaded);
      return { files: fetched || [], loading: fetched === null };
    },
  };
}
