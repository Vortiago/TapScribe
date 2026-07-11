// RED contract for #219 — "End meeting" with no live SpatialChat tab must not
// wedge in "Ending meeting…".
//
// Today onEnd (popup.js) unconditionally calls requestEndMeeting, which only
// bumps the meetingEndRequestedAt nonce. That nonce is consumed by a RUNNING
// content script (content.js storage.onChanged → drain → triggerPipeline →
// meetingEnd). If the SpatialChat tab was closed or crashed after Start, no
// content script consumes the nonce: the pipeline is never triggered, meetingEnd
// never updates, and the popup is dead-ended on "Ending meeting…". The popup
// already holds the same control client the content script uses, so when there
// is no live tab it can complete the End itself.
//
// The fix belongs in popup-actions.js (the DOM-free, fake-injectable effects
// module — cf. startMeeting/requestEndMeeting/dismissMeeting), as a single
// `endMeeting` dispatch that OWNS the live-vs-stale decision from the raw status
// snapshot, so onEnd is a thin pass-through (onEnd forwards latestStatus +
// Date.now() + the persisted meetingSessionId; it makes no decision itself).
//
// endMeeting({ control, storage, cfg, sessionId, snapshot, now }):
//   - live snapshot (a content script is polling)  → bump the meetingEndRequestedAt
//     nonce and let the content script own drain → trigger → meetingEnd (UNCHANGED
//     path; must NOT trigger directly here or it would skip the drain and truncate
//     the live taps' WAVs).
//   - stale / absent snapshot (no live content script) → complete the End itself:
//     trigger the pipeline via control.triggerPipeline(cfg, sessionId) and write the
//     terminal state the card reads — meetingActive:false + meetingEnd
//     { phase, sessionId, error, ts } — mirroring content.js finishEndMeeting so a
//     stale-tab End renders the SAME card as a normal End (started / busy / failed).
//
// RED today because popup-actions has no `endMeeting` export at all.

const test = require("node:test");
const assert = require("node:assert/strict");

let actions;
test.before(async () => { actions = await import("../popup-actions.js"); });

const CFG = { host: "localhost", port: 9999, useTls: false, token: "tok" };
const SID = "2026-06-19T10-00-00Z";
const NOW = 1750000000000;
const LIVE = { ts: NOW }; // snapshotIsLive(_, NOW) === true (age 0 <= 5000ms)
const STALE = { ts: NOW - 10000 }; // a content script that went away 10s ago

function fakeStorage() {
  const writes = [];
  return {
    writes,
    set: (items) => { writes.push(JSON.parse(JSON.stringify(items))); return Promise.resolve(); },
  };
}

/**
 * Fake control client capturing triggerPipeline calls; outcome/throw scriptable.
 * Default outcome is the REAL 202 token the client emits ("accepted"), NOT the
 * impl's derived phase — feeding "started" would let a mis-refactor
 * (outcome==="started"?…) mask the real 202 path through the else-branch.
 */
function fakeControl({ outcome = "accepted", throws = null } = {}) {
  const calls = [];
  return {
    calls,
    triggerPipeline: (cfg, sessionId, opts) => {
      calls.push({ cfg, sessionId, opts });
      return throws ? Promise.reject(throws) : Promise.resolve({ outcome });
    },
  };
}

/** Merge every storage.set payload into one view (fix may write once or twice). */
function merged(writes) { return Object.assign({}, ...writes); }
function anyWriteHasKey(writes, key) { return writes.some((w) => Object.prototype.hasOwnProperty.call(w, key)); }

// --- harm: no live tab → the popup completes the End itself -------------------

test("endMeeting with no live tab triggers the pipeline directly and writes the terminal meetingEnd", async () => {
  const storage = fakeStorage();
  const control = fakeControl({ outcome: "accepted" }); // the REAL 202 token

  await actions.endMeeting({ control, storage, cfg: CFG, sessionId: SID, snapshot: STALE, now: NOW });

  // The pipeline was triggered directly (the content script isn't there to do it),
  // under a timeout so a reachable-but-unresponsive recorder can't hang the await
  // and wedge the popup on "Ending meeting…" with both buttons disabled.
  assert.equal(control.calls.length, 1, "triggerPipeline must be called exactly once");
  assert.deepEqual(control.calls[0], { cfg: CFG, sessionId: SID, opts: { timeoutMs: 6000 } });

  // The terminal state the card reads is written, so the popup leaves
  // "Ending meeting…" instead of wedging. The REAL client returns
  // outcome "accepted" (202) — never "started" — so this pins that the
  // accepted→started mapping is validated against the real token.
  const m = merged(storage.writes);
  assert.equal(m.meetingActive, false, "meetingActive must be cleared");
  assert.ok(m.meetingEnd, "meetingEnd must be written");
  assert.equal(m.meetingEnd.phase, "started");
  assert.equal(m.meetingEnd.sessionId, SID);

  // It must NOT fall back to the nonce path — nothing would consume it.
  assert.equal(anyWriteHasKey(storage.writes, "meetingEndRequestedAt"), false,
    "a stale-tab End must not just bump the nonce");

  // The durable poll target must be PRESERVED: the card polls the pipeline via
  // meetingSessionId after End, so a write clearing it would strand the card.
  // content.js finishEndMeeting only flips meetingActive:false and keeps the id.
  assert.equal(anyWriteHasKey(storage.writes, "meetingSessionId"), false,
    "the stale End must not touch (and never clear) the durable poll-target meetingSessionId");
});

// --- guardrail: a live tab still owns drain → trigger → meetingEnd ------------

test("endMeeting with a live tab delegates via the nonce and does NOT trigger directly", async () => {
  const storage = fakeStorage();
  const control = fakeControl();

  await actions.endMeeting({ control, storage, cfg: CFG, sessionId: SID, snapshot: LIVE, now: NOW });

  // The content script is live: it must run drain → trigger. Triggering here
  // would skip the drain and truncate the live taps' WAVs.
  assert.equal(control.calls.length, 0, "must not trigger the pipeline while a tab is live");
  assert.deepEqual(merged(storage.writes), { meetingEndRequestedAt: NOW });
  assert.equal(anyWriteHasKey(storage.writes, "meetingEnd"), false,
    "the live content script owns the terminal meetingEnd, not the popup");
});

// --- guardrail: mirror finishEndMeeting's busy / failed phases ----------------

test("endMeeting surfaces a busy (409) trigger as meetingEnd.phase 'busy', still clearing meetingActive", async () => {
  const storage = fakeStorage();
  const control = fakeControl({ outcome: "busy" });

  await actions.endMeeting({ control, storage, cfg: CFG, sessionId: SID, snapshot: STALE, now: NOW });

  const m = merged(storage.writes);
  assert.equal(m.meetingEnd.phase, "busy", "a 409 busy must render as busy, not started");
  assert.equal(m.meetingActive, false);
});

test("endMeeting surfaces a failed trigger as meetingEnd.phase 'failed' and still leaves 'Ending' — no wedge", async () => {
  const storage = fakeStorage();
  const control = fakeControl({ throws: Object.assign(new Error("unreachable"), { kind: "network" }) });

  await actions.endMeeting({ control, storage, cfg: CFG, sessionId: SID, snapshot: STALE, now: NOW });

  const m = merged(storage.writes);
  assert.equal(m.meetingEnd.phase, "failed", "a trigger failure must be a terminal 'failed', not a wedge");
  assert.ok(m.meetingEnd.error, "the failure reason must be recorded for the card");
  assert.equal(m.meetingActive, false, "even on trigger failure the meeting is no longer active");
});

// --- guardrail: a mixed-content-blocked End renders the SAME failed card ------
// content.js's finishEndMeeting keys the failed reason off e.kind and emits the
// hardcoded TLS message, NOT the raw error text. The stale path must match that
// literal by construction so the two End cards can't silently drift on a future
// control-client message reword. The thrown message here deliberately DIFFERS
// from the expected literal — if endMeeting used raw errText(e) this would fail.
test("endMeeting maps a mixed-content-blocked throw to content.js's exact failed-card text (parity, not raw message)", async () => {
  const storage = fakeStorage();
  const control = fakeControl({
    throws: Object.assign(new Error("some future reworded control-client message"), { kind: "mixed-content-blocked" }),
  });

  await actions.endMeeting({ control, storage, cfg: CFG, sessionId: SID, snapshot: STALE, now: NOW });

  const m = merged(storage.writes);
  assert.equal(m.meetingEnd.phase, "failed");
  assert.equal(m.meetingEnd.error, "recorder is http:// on a non-trustworthy host — enable TLS",
    "must emit content.js's hardcoded TLS literal keyed off e.kind, not the raw thrown message");
});
