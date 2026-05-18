// In-page status pill — what the operator sees on the SpatialChat tab.
//
// The pill is the user-facing answer to "do I need to refresh, or is the
// bridge fine right now?" These tests pin the mapping from internal
// channel state to the green / yellow / red label the operator reads at
// a glance.

const test = require("node:test");
const assert = require("node:assert/strict");

const { createBridge } = require("./harness");

function pcmFrame(byteLen = 640) {
  return new ArrayBuffer(byteLen);
}

async function ready(bridge) {
  await bridge.flushMicrotasks();
}

test("pill is idle before any speaker is tapped", async () => {
  const b = createBridge();
  await ready(b);
  // No state changes have fired publishStatus yet — drive one manually
  // by posting a no-op (tap-start with no PCM creates a channel but no
  // socket; we instead use a ctx-state running to nudge publishStatus).
  b.post({ kind: "ctx-state", state: "running" });
  const ind = b.indicator();
  assert.ok(ind, "indicator host is mounted on documentElement");
  assert.equal(ind.kind, "idle");
  assert.match(ind.text, /idle/);
});

test("pill turns green and counts streams while audio is flowing", async () => {
  const b = createBridge();
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();

  const ind = b.indicator();
  assert.equal(ind.kind, "ok", "green dot while WS is OPEN and bytes flow");
  assert.match(ind.text, /1 stream/);
});

test("pill flips yellow when the /tap WS drops and we're reconnecting", async () => {
  const b = createBridge();
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws = b.lastSocket();
  ws.triggerOpen();
  // Unclean close mid-utterance — bridge schedules a reconnect.
  ws.triggerClose({ code: 1006, wasClean: false });

  const ind = b.indicator();
  // tap-ws-closed-1006 is a transport error and surfaces as the label;
  // reconnect is also scheduled. We pin the warn/err pair: this state
  // must NOT show as "ok" / "idle".
  assert.notEqual(ind.kind, "ok");
  assert.notEqual(ind.kind, "idle");
});

test("pill turns red and asks for a refresh when the audio context fails", async () => {
  const b = createBridge();
  await ready(b);
  b.post({ kind: "ctx-state", state: "failed" });

  const ind = b.indicator();
  assert.equal(ind.kind, "err");
  assert.match(ind.text, /refresh/);
  assert.match(ind.title, /Reload the SpatialChat tab/);
});

test("pill explains how to resume when the audio context is suspended", async () => {
  const b = createBridge();
  await ready(b);
  b.post({ kind: "ctx-state", state: "suspended" });

  const ind = b.indicator();
  assert.equal(ind.kind, "err");
  assert.match(ind.text, /suspended/);
  assert.match(ind.title, /Click anywhere/);
});

test("pill surfaces tap-auth-failed with token-fix guidance", async () => {
  const b = createBridge();
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws = b.lastSocket();
  // 4401 = recorder rejected the tap token. The bridge sets
  // ch.error="tap-auth-failed" and does NOT reconnect.
  ws.triggerClose({ code: 4401, wasClean: false });

  const ind = b.indicator();
  assert.equal(ind.kind, "err");
  assert.match(ind.text, /tap-auth-failed/);
  assert.match(ind.title, /token/);
});

test("pill recovers from suspended → running when the tab resumes", async () => {
  // Real user scenario: tab backgrounded, AudioContext auto-suspends,
  // user comes back and clicks → AudioContext resumes. The pill must
  // un-stick from the red "audio suspended" state and reflect the
  // current channel state instead. If the recovery transition is
  // missed (e.g. updateIndicator early-returns on stale
  // audioContextState) the pill would stay red even after capture
  // resumed, which is the worst kind of bug — silently misleading.
  const b = createBridge();
  await ready(b);
  b.post({ kind: "ctx-state", state: "suspended" });
  assert.equal(b.indicator().kind, "err");

  b.post({ kind: "ctx-state", state: "running" });
  // With no taps yet, the pill should now show idle, not still-err.
  assert.equal(b.indicator().kind, "idle");

  // And with an active stream after recovery it should reach green.
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();
  assert.equal(b.indicator().kind, "ok");
});

test("pill in multi-speaker scenarios: one error wins over other greens", async () => {
  // If Alice is streaming fine but Bob's token got revoked, the
  // operator needs to know SOMETHING is broken. The indicator must
  // surface the error rather than show green just because at least
  // one stream is healthy — silent partial failures are exactly the
  // class of bug we built the pill to prevent.
  const b = createBridge();
  await ready(b);
  b.post({ kind: "tap-start", identity: "alice", name: "Alice" });
  b.post({ kind: "pcm", identity: "alice", name: "Alice", buffer: pcmFrame() });
  const aliceWs = b.lastSocket();
  aliceWs.triggerOpen();
  // Alice alone: green.
  assert.equal(b.indicator().kind, "ok");

  // Bob joins; his /tap WS is rejected with 4401.
  b.post({ kind: "tap-start", identity: "bob", name: "Bob" });
  b.post({ kind: "pcm", identity: "bob", name: "Bob", buffer: pcmFrame() });
  const bobWs = b.lastSocket();
  bobWs.triggerClose({ code: 4401, wasClean: false });

  // The pill must flip to red — Alice being fine isn't a reason to
  // hide Bob's token problem.
  const ind = b.indicator();
  assert.equal(ind.kind, "err");
  assert.match(ind.text, /tap-auth-failed/);
});

test("pill re-mounts itself when SpatialChat removes its host from the DOM", async () => {
  // SpatialChat is an SPA; route changes can rewrite documentElement
  // children or shadow the indicator host out of existence. The
  // ensureIndicatorMounted guard checks `documentElement.contains(host)`
  // on every update — if the host is gone, it re-appends. Pin that
  // behaviour so a future "performance optimisation" that caches the
  // mount decision doesn't silently strand the indicator off-DOM.
  const b = createBridge();
  await ready(b);
  b.post({ kind: "ctx-state", state: "running" });
  assert.ok(b.indicator(), "indicator mounted on first publish");

  // Simulate SPA route change wiping the indicator off the tree. After
  // detach, indicator() can no longer find a host under documentElement.
  assert.equal(b.detachIndicator(), true, "host was attached before detach");
  assert.equal(b.indicator(), null, "indicator gone after SPA tree wipe");

  // The very next state-change must re-mount it — without an explicit
  // re-attach call, an operator who briefly navigates away from a chat
  // room and back would lose the status display permanently.
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  const ind = b.indicator();
  assert.ok(ind, "indicator re-mounted after subsequent state change");
  // And it should reflect the new state, not be a stale shell.
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();
  assert.equal(b.indicator().kind, "ok");
});
