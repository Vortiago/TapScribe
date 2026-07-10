// RED contract for issue #250 — a send failure PARTWAY through the drain
// flush must NOT drop the trailing PCM; the reconnect path has to retry the
// re-queued tail within the DRAIN_MAX_MS window (matching the C# TapStream,
// TapStream.cs:208-215).
//
// The bug (content.js onopen drain branch): after bufferFlush(ch) throws
// mid-flush it re-queues the unsent tail at the head, sets ch.error =
// "tap-send-failed", and returns with frames still buffered (content.js
// "Put it back at the head; reconnect path will try again"). But the very
// next line runs finalizeDrain(identity, ch) UNCONDITIONALLY whenever
// ch.draining — and finalizeDrain closes the WS and resetUtteranceState()
// empties ch.buffer. So the tail the flush deliberately kept for a retry is
// discarded immediately, even though the drain timer may have seconds left.
//
// The fix finalises the drain ONLY when the buffer actually emptied
// (ch.buffer.length === 0 after bufferFlush); otherwise it leaves draining
// set so the onclose -> scheduleReconnect ladder retries within the window.
//
// These tests drive a partial send failure by overriding the reconnect
// socket's `send` for one frame (no shared-harness change): buffer two
// frames, let the first send succeed and the second throw, so exactly the
// tail (frame 2) is at stake. `A` pins the mechanism (drain not finalised,
// tail retained, window still open); `B` pins the OUTCOME the operator cares
// about (the retained tail actually reaches the recorder on the retry, then
// the drain closes cleanly); `C` is the degenerate-fix guardrail — a clean
// full flush must STILL finalise, so "never finalise in onopen" can't pass.
//
// `D` covers the SYNCHRONOUS sibling: endUtterance's wsOpen branch flushes
// then closes+resets — the same partial-flush data-loss reached via a mute
// with an OPEN /tap (the onopen fix's mirror; must also retain the tail).
//
// `E`/`F` pin the AGGREGATION-layer harm the fix actually gates: finalizeDrain
// (and the DRAIN_MAX_MS backstop) call maybeFinishEndMeeting, so a mid-drain
// send failure with an End-meeting in progress must NOT let the meeting end
// with the tail lost NOR hang forever. `E` — tail delivers on a clean
// reconnect → the barrier finalises (one pipeline trigger) only after CLOSED;
// `F` — the tail NEVER lands → the DRAIN_MAX_MS backstop STILL finalises the
// meeting (no stuck "Ending…"). `F` is RED if line-586's backstop finish is
// removed; `E`'s post-partial-flush "no trigger yet" is RED if the onopen
// guard reverts to an unconditional finalize.

const test = require("node:test");
const assert = require("node:assert/strict");

const { createBridge } = require("./harness");

function pcmFrame(byteLen = 640) {
  return new ArrayBuffer(byteLen);
}

async function ready(bridge) {
  await bridge.flushMicrotasks();
}

// Build a channel into drain mode with two buffered frames and a live
// reconnect socket that has NOT opened yet — the exact state the onopen
// drain branch handles. Mirrors drain.test.js's "mute while reconnecting
// enters drain mode" setup. Returns the reconnect socket (ws2).
function drainWithTwoBufferedFrames(b) {
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  ws1.triggerClose({ code: 1006, wasClean: false }); // link blip
  // While reconnecting, two frames buffer locally.
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  // Mute before the reconnect lands -> drain mode.
  b.post({ kind: "mute", identity: "u1", muted: true });
  const chDraining = b.status().channels.find((c) => c.identity === "u1");
  assert.equal(chDraining.draining, true, "precondition: drain mode entered");
  assert.equal(chDraining.bufferedFrames, 2, "precondition: two frames buffered");
  // The reconnect socket the drain ladder schedules.
  b.clock.tick(500);
  const ws2 = b.lastSocket();
  assert.notEqual(ws2, ws1, "precondition: reconnect socket created for the drain");
  return ws2;
}

// Make the given 1-indexed send() calls throw; the rest pass through. Frames
// only pile up on an OPEN socket via a caught send failure, so this both
// buffers frames on an open WS AND fails one frame of the later mute-flush so
// it fails PARTWAY — needed to reach the SYNCHRONOUS wsOpen drain path.
function failSendOnCalls(ws, indices) {
  const doomed = new Set(indices);
  const orig = ws.send.bind(ws);
  let n = 0;
  ws.send = (buf) => {
    n += 1;
    if (doomed.has(n)) throw new Error("tap-send-failed (simulated, call " + n + ")");
    return orig(buf);
  };
}

function triggerCalls(bridge) {
  return bridge.fetches().filter((c) => /\/pipeline$/.test(c.url));
}

test("a send failure mid-drain keeps the tail buffered and does NOT finalise the drain", () => {
  const b = createBridge();
  return ready(b).then(() => {
    const ws2 = drainWithTwoBufferedFrames(b);
    failSendOnCalls(ws2, [2]); // frame 1 sends, frame 2 throws -> tail re-queued
    ws2.triggerOpen();

    // Bug: onopen finalises unconditionally -> WS closed, buffer emptied,
    // draining cleared, drain timer cancelled. The tail is gone.
    assert.equal(ws2.closed, null, "the reconnect WS must stay open, not be closed as drain-complete");
    const ch = b.status().channels.find((c) => c.identity === "u1");
    assert.equal(ch.draining, true, "drain must stay armed so the reconnect path retries the tail");
    assert.equal(ch.bufferedFrames, 1, "the un-sent tail frame must remain buffered, not be discarded");
    assert.ok(b.clock.pending() > 0, "the drain timer must still be pending — the window was not abandoned");
  });
});

test("after a mid-drain send failure the reconnect retry delivers the tail, then closes cleanly", () => {
  const b = createBridge();
  return ready(b).then(() => {
    const ws2 = drainWithTwoBufferedFrames(b);
    failSendOnCalls(ws2, [2]);
    ws2.triggerOpen(); // partial flush: frame 1 out, frame 2 re-queued
    // The dead link that failed the send now drops -> the drain reconnect
    // ladder should schedule another attempt within the remaining window.
    ws2.triggerClose({ code: 1006, wasClean: false });
    b.clock.tick(500);
    const ws3 = b.lastSocket();
    assert.notEqual(ws3, ws2, "a fresh reconnect socket must be created within the drain window");
    ws3.triggerOpen();

    // The retained tail frame reaches the recorder on the healthy socket,
    // and only THEN does the drain finalise with a clean close.
    assert.equal(ws3.sent.length, 1, "the retained tail frame must be flushed on the retry");
    assert.equal(ws3.closed.reason, "muted", "the drain closes cleanly only after the tail is delivered");
    const ch = b.status().channels.find((c) => c.identity === "u1");
    assert.equal(ch.draining, false, "draining cleared once the tail is delivered");
    assert.equal(ch.bufferedFrames, 0, "buffer empty once the tail is delivered");
  });
});

test("a fully-successful drain flush still finalises immediately (degenerate-fix guardrail)", () => {
  const b = createBridge();
  return ready(b).then(() => {
    const ws2 = drainWithTwoBufferedFrames(b);
    ws2.triggerOpen(); // no injected failure -> both frames flush cleanly

    assert.equal(ws2.sent.length, 2, "both buffered frames flushed on the drain reopen");
    assert.equal(ws2.closed.reason, "muted", "the drain finalises with a clean close once the buffer empties");
    const ch = b.status().channels.find((c) => c.identity === "u1");
    assert.equal(ch.draining, false, "draining cleared after a clean finalise");
    assert.equal(ch.bufferedFrames, 0, "buffer empty after a clean finalise");
  });
});

test("D: mute with an OPEN /tap and a partial flush keeps the tail buffered and enters drain (sync sibling)", () => {
  const b = createBridge();
  return ready(b).then(() => {
    b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
    b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
    const ws = b.lastSocket();
    ws.triggerOpen(); // flushes the first frame; buffer now empty, WS OPEN

    // Frames pile up on an OPEN socket only when a send throws (the pcm handler
    // catches and buffers). Fail calls 1,2 to buffer two frames, then fail only
    // call 4 so the later mute-flush is PARTIAL: call 3 = flush frame A (ok),
    // call 4 = flush frame B (throws → tail re-queued).
    failSendOnCalls(ws, [1, 2, 4]);
    b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
    b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
    b.publishTick(); // the buffering branch doesn't publish; refresh the snapshot
    const before = b.status().channels.find((c) => c.identity === "u1");
    assert.equal(before.bufferedFrames, 2, "precondition: two frames buffered on the OPEN WS");

    b.post({ kind: "mute", identity: "u1", muted: true });

    // Bug (sync path): endUtterance's wsOpen branch closed+reset
    // unconditionally after bufferFlush, discarding the re-queued tail. The
    // fix leaves draining armed so the reconnect ladder retries it.
    assert.equal(ws.closed, null, "the OPEN WS must NOT be closed — the tail is still buffered");
    const ch = b.status().channels.find((c) => c.identity === "u1");
    assert.equal(ch.draining, true, "drain must be armed so the reconnect path retries the tail");
    assert.equal(ch.bufferedFrames, 1, "the un-sent tail frame stays buffered, not discarded");
    assert.ok(b.clock.pending() > 0, "the drain timer is pending — the window was not abandoned");
    assert.equal(ws.sent.length, 2, "the initial frame + the first flush frame reached the recorder");
  });
});

test("E: End-meeting mid-drain + partial flush finalises the meeting only AFTER the retried tail lands", async () => {
  const b = createBridge({ settings: { meetingSessionId: "sess-1" } });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  ws1.triggerClose({ code: 1006, wasClean: false }); // blip → reconnecting
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });

  b.requestEndMeeting();
  assert.equal(triggerCalls(b).length, 0, "u1 draining → no trigger yet");

  // Drain reconnect lands but the SECOND send throws → tail re-queued.
  b.clock.tick(500);
  const ws2 = b.lastSocket();
  assert.notEqual(ws2, ws1, "drain reconnect opened a new WS");
  failSendOnCalls(ws2, [2]);
  ws2.triggerOpen();
  // A partial flush must NOT complete the barrier — ending the meeting now
  // would process a Session missing its last audio.
  assert.equal(triggerCalls(b).length, 0, "partial flush keeps the channel draining → no trigger");

  // The dead link drops; the drain ladder schedules another attempt, which
  // delivers the retained tail and closes cleanly.
  ws2.triggerClose({ code: 1006, wasClean: false });
  b.clock.tick(500);
  const ws3 = b.lastSocket();
  assert.notEqual(ws3, ws2, "a fresh reconnect socket was created within the drain window");
  ws3.triggerOpen();

  assert.equal(ws3.sent.length, 1, "the retained tail frame reached the recorder on the retry");
  assert.equal(triggerCalls(b).length, 1, "the meeting finalised once every tap CLOSED with its tail delivered");
});

test("F: End-meeting mid-drain whose tail NEVER lands still finalises via the DRAIN_MAX_MS backstop", async () => {
  const b = createBridge({ settings: { meetingSessionId: "sess-1" } });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  ws1.triggerClose({ code: 1006, wasClean: false }); // blip → reconnecting
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() }); // buffered tail

  b.requestEndMeeting();
  assert.equal(triggerCalls(b).length, 0, "u1 draining → no trigger yet");

  // The recorder never comes back: the drain reconnect never opens, so the
  // tail is never delivered. The DRAIN_MAX_MS backstop must give up on the
  // tail AND finish the End-meeting barrier — otherwise the meeting hangs in
  // "Ending…" forever.
  b.clock.tick(8000 + 500);
  assert.equal(triggerCalls(b).length, 1, "the backstop finalised the meeting despite the lost tail");
});
