// Tests for "drain on mute" behaviour in content.js.
//
// The contract we're locking in:
//   - Mute with no buffered PCM → close /tap immediately (no drain).
//   - Mute with buffered PCM and WS open → flush buffer, then close.
//   - Mute with buffered PCM and WS reconnecting → enter drain mode,
//     keep the reconnect ladder running, flush on next open, then
//     close cleanly. Surfaces "draining" in the status snapshot.
//   - Drain hits DRAIN_MAX_MS (8s) with no successful open →
//     ch.error = "drain-timeout", buffer discarded, channel returns idle.
//   - Unmute during drain → abandon drain, close cleanly, ready for a
//     fresh utterance.
//   - tap-stop → force close regardless of buffer (speaker has left).

const test = require("node:test");
const assert = require("node:assert/strict");

const { createBridge, FakeWebSocket } = require("./harness");

// A 20ms int16 mono 16kHz frame = 640 bytes. Tests send small ArrayBuffers
// that don't have to be real audio — the bridge treats them as opaque PCM.
function pcmFrame(byteLen = 640) {
  return new ArrayBuffer(byteLen);
}

function setupChannel(bridge, { identity = "u1", name = "Alice" } = {}) {
  bridge.post({ kind: "tap-start", identity, name });
}

async function ready(bridge) {
  // The script reads chrome.storage settings via Promise.then. We need
  // those microtasks to flush before it'll accept PCM (gated by
  // settingsReady).
  await bridge.flushMicrotasks();
}

test("mute with no buffered PCM closes /tap immediately", async () => {
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws = b.lastSocket();
  assert.ok(ws, "WS opened on first PCM");
  ws.triggerOpen();
  // Pending PCM frame was sent on open
  assert.equal(ws.sent.length, 1, "first frame sent");

  b.post({ kind: "mute", identity: "u1", muted: true });

  assert.deepEqual(ws.closed, { code: 1000, reason: "muted", wasClean: true });
  assert.equal(b.openSockets().length, 1, "no new WS opened by mute");
  const snap = b.status();
  const ch = snap.channels.find((c) => c.identity === "u1");
  assert.equal(ch.muted, true);
  assert.equal(ch.draining, false, "no drain — buffer was empty");
});

test("mute with WS open and buffered PCM flushes then closes", async () => {
  // Sequence: PCM lands, WS opens, link blips closed, more PCM arrives
  // and buffers, link recovers and WS reopens, mute fires. The bridge
  // should flush the buffer through the open WS and then close.
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  ws1.triggerClose({ code: 1006, wasClean: false }); // blip

  // While reconnecting, more PCM arrives — buffered
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });

  // Fire the reconnect timer
  b.clock.tick(500);
  const ws2 = b.lastSocket();
  assert.notEqual(ws2, ws1, "new WS for reconnect");
  ws2.triggerOpen();
  // bufferFlush sent the two buffered frames on open
  assert.equal(ws2.sent.length, 2, "buffered frames flushed on reopen");

  // Now mute with WS open and no leftover buffer — should close cleanly
  b.post({ kind: "mute", identity: "u1", muted: true });
  assert.equal(ws2.closed.reason, "muted");
});

test("mute while WS is reconnecting enters drain mode", async () => {
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  ws1.triggerClose({ code: 1006, wasClean: false }); // blip
  // While reconnecting, more PCM arrives — buffered locally
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });

  // Mute fires before reconnect lands → drain
  b.post({ kind: "mute", identity: "u1", muted: true });

  const snapAfterMute = b.status();
  const ch = snapAfterMute.channels.find((c) => c.identity === "u1");
  assert.equal(ch.muted, true);
  assert.equal(ch.draining, true, "drain mode entered");
  assert.equal(ch.bufferedFrames, 2);

  // Reconnect should still fire despite muted=true (drain exception)
  b.clock.tick(500);
  const ws2 = b.lastSocket();
  assert.notEqual(ws2, ws1, "reconnect socket created during drain");
  ws2.triggerOpen();

  // onopen flushes the buffer and then closes
  assert.equal(ws2.sent.length, 2, "buffered audio flushed to recorder");
  assert.equal(ws2.closed.reason, "muted", "WS closed after drain flush");

  const snapAfterDrain = b.status();
  const ch2 = snapAfterDrain.channels.find((c) => c.identity === "u1");
  assert.equal(ch2.draining, false, "draining flag cleared after finalize");
  assert.equal(ch2.bufferedFrames, 0);
});

test("drain times out after DRAIN_MAX_MS and surfaces error", async () => {
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  ws1.triggerClose({ code: 1006, wasClean: false });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });

  b.post({ kind: "mute", identity: "u1", muted: true });

  // Fail every reconnect attempt the drain schedules. The drain has
  // 8s; reconnect backoff starts at ~200ms and grows. We tick a little
  // past 8s and the drain timer should fire even if reconnects are
  // still pending.
  for (let i = 0; i < 8; i++) {
    b.clock.tick(1100);
    const ws = b.lastSocket();
    if (ws && ws !== ws1 && !ws.closed) {
      ws.triggerError();
      ws.triggerClose({ code: 1006, wasClean: false });
    }
  }
  // Past 8s total — drain timer should have fired
  const snap = b.status();
  const ch = snap.channels.find((c) => c.identity === "u1");
  assert.equal(ch.error, "drain-timeout", "drain timeout surfaced as error");
  assert.equal(ch.draining, false, "drain flag cleared after timeout");
  assert.equal(ch.bufferedFrames, 0, "buffer discarded after timeout");
});

test("unmute mid-drain abandons the drain and clears state", async () => {
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  ws1.triggerClose({ code: 1006, wasClean: false });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });

  b.post({ kind: "mute", identity: "u1", muted: true });
  let ch = b.status().channels.find((c) => c.identity === "u1");
  assert.equal(ch.draining, true);

  // Speaker un-mutes before reconnect lands. Bridge should abandon the
  // drain so the next PCM starts a fresh utterance.
  b.post({ kind: "mute", identity: "u1", muted: false });
  ch = b.status().channels.find((c) => c.identity === "u1");
  assert.equal(ch.muted, false);
  assert.equal(ch.draining, false);
  assert.equal(ch.bufferedFrames, 0);

  // Next PCM frame mints a new utterance_id (different from the first
  // one) — confirms state was reset, not just the flag flipped.
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws2 = b.lastSocket();
  assert.notEqual(ws2.url, ws1.url, "new utterance_id after drain abandon");
});

test("tap-stop force-closes even with buffered PCM (no drain)", async () => {
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  ws1.triggerClose({ code: 1006, wasClean: false });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });

  b.post({ kind: "tap-stop", identity: "u1" });

  // No drain timer pending; the channel is gone.
  assert.equal(b.clock.pending(), 0, "no lingering timers after tap-stop");
  const snap = b.status();
  assert.equal(snap.channels.length, 0, "channel removed");
});

test("trailing pcm after tap-stop does not resurrect the tap", async () => {
  // The room-teardown e2e flake (PR #344 CI): the page's audio pipeline
  // tears down asynchronously, so a pcm frame can trail the tap-stop.
  // When tap-stop DELETED the channel, ensureChannel resurrected it
  // (stopped: false) on that trailing frame and opened a brand-new /tap
  // with a fresh utterance id for a speaker who had left. The channel
  // now tombstones (stopped: true, kept in the map) and pcm drops.
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();

  b.post({ kind: "tap-stop", identity: "u1" });
  assert.equal(ws1.closed.code, 1000, "tap-stop closed the live WS");
  const socketsAfterStop = b.openSockets().length;

  // The trailing frame from the tearing-down pipeline.
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });

  assert.equal(
    b.openSockets().length,
    socketsAfterStop,
    "no new /tap WS for a departed speaker",
  );
  assert.equal(b.clock.pending(), 0, "no reconnect timer armed by the trailing frame");
  const snap = b.status();
  assert.equal(
    snap.channels.find((c) => c.identity === "u1"),
    undefined,
    "tombstone stays hidden from the status snapshot",
  );

  // A genuine rejoin re-arms via tap-start, and the next frame opens a
  // fresh tap — the tombstone must not brick the identity forever.
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  assert.equal(
    b.openSockets().length,
    socketsAfterStop + 1,
    "rejoin opens a fresh /tap",
  );
  const ws2 = b.lastSocket();
  assert.notEqual(ws2.url, ws1.url, "rejoin minted a new utterance_id");
});

test("drain resets reconnectAttempt for a fresh fast retry", async () => {
  // Regression: without this reset, if the reconnect ladder had climbed
  // to attempt N before mute, the next attempt would still be at delay
  // f(N) — up to 5s — burning most of the 8s drain budget. Entering
  // drain must reset reconnectAttempt so the next schedule starts at
  // ~200ms.
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();
  // Burn several failed reconnects so the ladder climbs to its cap.
  for (let i = 0; i < 5; i++) {
    b.lastSocket().triggerClose({ code: 1006, wasClean: false });
    b.clock.tick(6000); // overshoot every backoff delay
  }
  // After the loop the last socket is CONNECTING (never opened) and
  // ch.reconnectAttempt is at its cap. The next scheduleReconnect call
  // would therefore use ~5s delay — without the fix, mute would burn
  // most of its 8s drain budget on backoff.

  // Buffer some PCM and mute → enter drain mode and reset the ladder.
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.post({ kind: "mute", identity: "u1", muted: true });

  const socketsBefore = b.openSockets().length;
  // Fail the in-flight CONNECTING socket. Its onclose will schedule a
  // reconnect — and with the reset, that schedule uses delay ~200ms.
  b.lastSocket().triggerClose({ code: 1006, wasClean: false });
  // 500ms is enough for the reset-200ms backoff (plus jitter) but not
  // enough for the un-reset ~5000ms cap. So a new socket appearing here
  // proves the reset happened.
  b.clock.tick(500);
  const socketsAfter = b.openSockets().length;
  assert.ok(socketsAfter > socketsBefore,
    "drain retry fired within 500ms (ladder was reset)");
});
