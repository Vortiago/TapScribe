// @ts-check
// SpatialChat Bridge — taps-view.js (pure "Active taps" view-model)
//
// The popup's "Active taps (per speaker)" decision logic, extracted DOM-free so
// the staleness rule + per-row state labels are unit-testable with plain inputs
// (no jsdom, no chrome stubs) — the same pure-mapper split as pipeline-view.js.
// The popup's DOM shell (popup.js) feeds the latest `bridgeStatus` snapshot + a
// clock and renders the channel rows, OR the empty state when the snapshot is
// stale (the SpatialChat tab that wrote it has gone away).
//
// Why staleness matters: a `bridgeStatus` snapshot lives in chrome.storage.local
// and OUTLIVES the content script that wrote it. When the SpatialChat tab is
// closed (or crashes), content.js stops publishing but its last snapshot stays
// in storage forever — so the popup would keep rendering departed speakers as
// OPEN/active taps with no tab open at all. The fix is liveness-by-recency:
// content.js refreshes the snapshot's `ts` at least every STATUS_HEARTBEAT_MS
// while it runs (even when nothing observable changed — see publishStatus in
// content.js), so a snapshot older than STALE_AFTER_MS means the writer is gone.

// Max age (ms) a snapshot may reach before the popup treats it as a dead tab's
// leftover rather than live taps. Must stay comfortably above content.js's
// STATUS_HEARTBEAT_MS (currently 2000) plus the popup's storage-poll interval
// (1500) so a live-but-quiet (all-muted) tab is never falsely flagged stale.
export const STALE_AFTER_MS = 5000;

/**
 * The per-row state label shown in the popup's "state" column.
 * @param {{ error?: string | null, draining?: boolean, muted?: boolean }} c
 * @returns {string}
 */
export function tapStateLabel(c) {
  if (c.error) return c.error;
  if (c.draining) return "draining";
  if (c.muted) return "muted";
  return "active";
}

/**
 * Is the snapshot fresh enough that its channels reflect a still-running
 * content script? A snapshot with no numeric `ts` (shouldn't happen — every
 * buildStatusSnapshot stamps one — but be defensive) is treated as NOT live so
 * a leftover can never render as live taps.
 * @param {{ ts?: number } | null | undefined} snapshot
 * @param {number} now epoch ms (Date.now() at the call site)
 * @param {number} [maxAgeMs]
 * @returns {boolean}
 */
export function snapshotIsLive(snapshot, now, maxAgeMs = STALE_AFTER_MS) {
  if (!snapshot || typeof snapshot.ts !== "number") return false;
  return now - snapshot.ts <= maxAgeMs;
}
