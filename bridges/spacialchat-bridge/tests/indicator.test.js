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
