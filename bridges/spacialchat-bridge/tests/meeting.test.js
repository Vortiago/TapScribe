// Meeting-routing tests for the SpatialChat bridge content script.
//
// The bracketed-meeting model (#131/#133) routes taps into an isolated
// detached Session: while a meeting is active, every /tap URL the content
// script opens carries `&session=<id>` so the audio lands in that Session
// instead of the Recorder's global one. The popup mints the detached
// Session and persists `meetingSessionId` to chrome.storage.local; the
// content script reads it (at boot and live via storage.onChanged) and is
// the source of truth for tap routing. With no meeting active, taps carry
// no `session` param and fall back to the global Session, unchanged.
//
// These tests drive the content-script harness (no real browser): they
// assert on the observable /tap URL, never on private state.

const test = require("node:test");
const assert = require("node:assert/strict");

const { createBridge } = require("./harness");

function pcmFrame(byteLen = 640) {
  return new ArrayBuffer(byteLen);
}

async function ready(bridge) {
  await bridge.flushMicrotasks();
}

function sessionParam(ws) {
  return new URL(ws.url).searchParams.get("session");
}

test("a tap opened during an active meeting carries &session=<id>", async () => {
  // Meeting already active at boot (popup persisted meetingSessionId
  // before the tab loaded). The first /tap open must route into it.
  const b = createBridge({ settings: { meetingSessionId: "2026-06-19T10-00-00Z" } });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws = b.lastSocket();
  assert.ok(ws, "a /tap WS was opened");
  assert.equal(sessionParam(ws), "2026-06-19T10-00-00Z", "tap routed into the meeting Session");
});

test("with no meeting active, a tap carries no session param (global fallback)", async () => {
  const b = createBridge(); // no meetingSessionId in storage
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws = b.lastSocket();
  assert.ok(ws, "a /tap WS was opened");
  assert.equal(sessionParam(ws), null, "no session param → Recorder's global Session");
});

test("a reconnect after a blip still carries &session=<id> for the same utterance", async () => {
  // The whole meeting must stay in one Session even across a transient WS
  // blip: the reconnect reuses the utterance's affiliation, so its URL
  // carries both the same utterance_id and the same session.
  const b = createBridge({ settings: { meetingSessionId: "sess-mtg" } });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  assert.equal(sessionParam(ws1), "sess-mtg", "initial open routed into the meeting");

  // Network blip — WS closes uncleanly, the reconnect ladder fires.
  ws1.triggerClose({ code: 1006, wasClean: false });
  b.clock.tick(500);

  const ws2 = b.lastSocket();
  assert.notEqual(ws2, ws1, "reconnect opened a new WS");
  assert.equal(sessionParam(ws2), "sess-mtg", "reconnect stayed in the meeting Session");
});

test("Start meeting picked up live from storage routes the NEXT utterance", async () => {
  // The popup persists meetingSessionId to chrome.storage while the
  // SpatialChat tab is already open. The content script must pick it up
  // via storage.onChanged — no tab reload — and route the next utterance.
  const b = createBridge(); // boots with no meeting
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });

  // Speaks before the meeting: global Session.
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  assert.equal(sessionParam(b.lastSocket()), null, "pre-meeting tap is global");

  // Operator clicks Start meeting in the popup → storage.onChanged fires.
  b.startMeeting("sess-live");

  // End the current utterance and start a fresh one — it routes into the meeting.
  b.post({ kind: "mute", identity: "u1", muted: true });
  b.post({ kind: "mute", identity: "u1", muted: false });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  assert.equal(sessionParam(b.lastSocket()), "sess-live", "next utterance routed into the meeting");
});

test("affiliation is fixed for the life of an utterance; the next utterance reflects the change", async () => {
  // The Recorder snapshots Session at WS open and stitches reconnects by
  // utterance_id, so an utterance must not split across two Sessions. If a
  // meeting ends mid-utterance, that utterance keeps its Session across a
  // reconnect; only the NEXT utterance falls back to the global Session.
  const b = createBridge({ settings: { meetingSessionId: "sess-x" } });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  assert.equal(sessionParam(ws1), "sess-x");

  // Meeting ends mid-utterance (popup cleared meetingSessionId in storage).
  b.endMeeting();

  // A blip + reconnect within the SAME utterance keeps the affiliation.
  ws1.triggerClose({ code: 1006, wasClean: false });
  b.clock.tick(500);
  assert.equal(sessionParam(b.lastSocket()), "sess-x", "open utterance is not re-routed");

  // The next utterance (after mute → unmute) falls back to the global Session.
  b.post({ kind: "mute", identity: "u1", muted: true });
  b.post({ kind: "mute", identity: "u1", muted: false });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  assert.equal(sessionParam(b.lastSocket()), null, "next utterance is global after the meeting ended");
});

test("the in-page pill reflects capture into a bracketed meeting while streaming", async () => {
  const b = createBridge({ settings: { meetingSessionId: "sess-pill" } });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();

  const ind = b.indicator();
  assert.equal(ind.kind, "ok", "streaming is the happy path");
  assert.match(ind.text, /meeting/i, "the pill label signals the bracketed Session");
});

test("the in-page pill shows the meeting is live even before anyone speaks", async () => {
  // A meeting is started but nobody is talking yet: the pill must still
  // tell the operator the bracket is live (not the plain global 'idle').
  const b = createBridge({ settings: { meetingSessionId: "sess-pill" } });
  await ready(b);
  b.startMeeting("sess-pill"); // re-publish to refresh the pill with no channels
  const ind = b.indicator();
  assert.match(ind.text, /meeting/i, "idle-but-bracketed is distinct from global idle");
});

test("with no meeting active, the streaming pill does not mention a meeting", async () => {
  const b = createBridge();
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();

  const ind = b.indicator();
  assert.equal(ind.kind, "ok");
  assert.doesNotMatch(ind.text, /meeting/i, "global capture stays unlabelled");
});

test("the tab title is tagged `mtg` while capture is bracketed into a meeting Session", async () => {
  // S18: glancing at the title bar must tell the operator the tab is recording
  // into a named Session, not the global default. The suffix is built only in
  // the 2 Hz publish/title tick, so fire it explicitly.
  const b = createBridge({ settings: { meetingSessionId: "sess-x" } });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();

  b.publishTick();
  assert.match(b.title(), /\[tap mtg /, "tab title carries the mtg tag for a bracketed Session");
});

test("the tab title has no `mtg` tag for global (un-bracketed) capture", async () => {
  // The negative half of S18: identical capture, no meeting → the title shows
  // the tap status but NOT the mtg tag, so the tag is a real signal of the
  // bracket and not always-on.
  const b = createBridge();
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();

  b.publishTick();
  const t = b.title();
  assert.match(t, /\[tap /, "title still shows the tap status");
  assert.doesNotMatch(t, /\bmtg\b/, "no mtg tag for un-bracketed global capture");
});

test("a SpatialChat room change performs no session rotation, and the meeting persists", async () => {
  // The legacy auto-rotate is gone: a room change must POST nothing. The
  // active meeting's detached Session persists across the swap (PRD §5).
  const b = createBridge({ settings: { meetingSessionId: "sess-x" } });
  await ready(b);

  b.post({ kind: "room-changed" });
  assert.equal(b.fetches().length, 0, "no control POST fired on a room change");

  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  assert.equal(sessionParam(b.lastSocket()), "sess-x", "meeting Session persists across the room swap");
});

test("the status snapshot carries the bracketed-meeting state", async () => {
  const b = createBridge({ settings: { meetingSessionId: "sess-snap" } });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });

  const snap = b.status();
  assert.equal(snap.meetingActive, true, "snapshot flags an active meeting");
  assert.equal(snap.meetingSessionId, "sess-snap", "snapshot carries the meeting Session id");
});

test("the snapshot reports no meeting when none is active", async () => {
  const b = createBridge();
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });

  const snap = b.status();
  assert.equal(snap.meetingActive, false);
  assert.equal(snap.meetingSessionId, null);
});
