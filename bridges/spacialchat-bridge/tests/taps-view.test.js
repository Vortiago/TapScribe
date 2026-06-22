// Tests for taps-view.js — the pure "Active taps" view-model. No DOM, no mocks:
// feed a bridgeStatus snapshot + a clock, assert the staleness verdict and the
// per-row state label. The popup's DOM shell (popup.js) renders the channel
// rows only while snapshotIsLive() is true; once the SpatialChat tab that wrote
// the snapshot goes away, content.js stops refreshing `ts` and the popup must
// fall back to the empty state instead of showing departed speakers as live
// OPEN/active taps (the reported bug: taps shown with no tab open).

const test = require("node:test");
const assert = require("node:assert/strict");

// taps-view.js is an ES module; load it via dynamic import once.
let snapshotIsLive, tapStateLabel, STALE_AFTER_MS;
test.before(async () => {
  ({ snapshotIsLive, tapStateLabel, STALE_AFTER_MS } = await import("../taps-view.js"));
});

// ---- snapshotIsLive: the staleness rule -----------------------------------

test("a just-written snapshot is live", () => {
  const now = 1_000_000;
  assert.equal(snapshotIsLive({ ts: now }, now), true);
});

test("a snapshot within the freshness window is live", () => {
  const now = 1_000_000;
  assert.equal(snapshotIsLive({ ts: now - (STALE_AFTER_MS - 1) }, now), true);
});

test("a snapshot exactly at the threshold is still live (inclusive)", () => {
  const now = 1_000_000;
  assert.equal(snapshotIsLive({ ts: now - STALE_AFTER_MS }, now), true);
});

test("a snapshot older than the threshold is stale — the closed-tab leftover", () => {
  // The bug: the SpatialChat tab closed, content.js stopped publishing, but its
  // last snapshot lingers in chrome.storage.local. One heartbeat past the
  // window and the popup must treat it as a dead tab, NOT live taps.
  const now = 1_000_000;
  assert.equal(snapshotIsLive({ ts: now - (STALE_AFTER_MS + 1) }, now), false);
});

test("a long-dead snapshot (tab closed ages ago) is stale", () => {
  const now = 1_000_000;
  assert.equal(snapshotIsLive({ ts: now - 600_000 }, now), false);
});

test("a snapshot with no ts is treated as stale (defensive)", () => {
  const now = 1_000_000;
  assert.equal(snapshotIsLive({}, now), false);
  assert.equal(snapshotIsLive({ ts: "nope" }, now), false);
});

test("null / undefined snapshot is stale", () => {
  const now = 1_000_000;
  assert.equal(snapshotIsLive(null, now), false);
  assert.equal(snapshotIsLive(undefined, now), false);
});

test("an explicit maxAgeMs override is honoured", () => {
  const now = 1_000_000;
  assert.equal(snapshotIsLive({ ts: now - 100 }, now, 50), false);
  assert.equal(snapshotIsLive({ ts: now - 100 }, now, 200), true);
});

// ---- tapStateLabel: the per-row "state" column ----------------------------

test("tapStateLabel prefers error, then draining, then muted, else active", () => {
  assert.equal(tapStateLabel({ error: "backpressure" }), "backpressure");
  assert.equal(tapStateLabel({ draining: true }), "draining");
  assert.equal(tapStateLabel({ muted: true }), "muted");
  assert.equal(tapStateLabel({}), "active");
  // error wins over everything else
  assert.equal(tapStateLabel({ error: "x", draining: true, muted: true }), "x");
});
