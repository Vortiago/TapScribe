// End-meeting tests for the SpatialChat bridge content script (#134).
//
// "End meeting" cleanly closes the bracketed meeting and fires the
// Recorder's end-of-meeting pipeline for the meeting's detached Session.
// The popup signals the content script (via chrome.storage); the content
// script — which owns the /tap WebSockets and outlives the ephemeral popup
// — closes every open channel honouring the existing Drain-on-mute path,
// waits until ALL taps have reached CLOSED (a close-all barrier so the last
// Utterance's WAV is finalised first), then calls the shared control
// client's triggerPipeline(meetingSessionId). The trigger carries no body
// (the Recorder uses operator defaults). A 409 (Session busy) is surfaced,
// not auto-hammered. After ending, meetingSessionId is cleared so capture
// falls back to the global Session.
//
// These drive the content-script harness and assert on observable effects:
// the /tap WS lifecycle and the trigger request that goes out.

const test = require("node:test");
const assert = require("node:assert/strict");

const { createBridge } = require("./harness");

function pcmFrame(byteLen = 640) {
  return new ArrayBuffer(byteLen);
}

async function ready(bridge) {
  await bridge.flushMicrotasks();
}

function triggerCalls(bridge) {
  return bridge.fetches().filter((c) => /\/pipeline$/.test(c.url));
}

test("End meeting closes the open tap, then triggers the pipeline on the meeting Session", async () => {
  const b = createBridge({
    settings: { meetingSessionId: "sess-1", recorderHost: "localhost", recorderPort: 9999, tapToken: "tok" },
  });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws = b.lastSocket();
  ws.triggerOpen();
  assert.equal(triggerCalls(b).length, 0, "no trigger before End");

  b.requestEndMeeting();

  assert.ok(ws.closed, "the open tap was closed");
  const calls = triggerCalls(b);
  assert.equal(calls.length, 1, "exactly one pipeline trigger after CLOSED");
  assert.equal(calls[0].url, "http://localhost:9999/api/tap/sessions/sess-1/pipeline");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers.Authorization, "Bearer tok");
  assert.equal(calls[0].options.body, undefined, "trigger sends no model/summarizer/prompt body");
});

test("End meeting drains buffered trailing PCM before closing, and triggers only after CLOSED", async () => {
  const b = createBridge({ settings: { meetingSessionId: "sess-1" } });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  ws1.triggerClose({ code: 1006, wasClean: false }); // blip → reconnecting
  // Trailing PCM arrives while the WS is down — buffered locally.
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });

  b.requestEndMeeting();

  // The channel is draining (WS not yet up): the trigger must NOT fire yet —
  // the last Utterance's WAV isn't finalised.
  assert.equal(triggerCalls(b).length, 0, "no trigger while a tap is still draining");

  // Reconnect lands: the buffered tail is flushed, then the WS closes.
  b.clock.tick(500);
  const ws2 = b.lastSocket();
  assert.notEqual(ws2, ws1, "drain reconnect opened a new WS");
  ws2.triggerOpen();
  assert.equal(ws2.sent.length, 2, "trailing buffered PCM flushed before close");
  assert.ok(ws2.closed, "WS closed after the drain flush");

  // Only now — after every tap reached CLOSED — does the trigger fire.
  assert.equal(triggerCalls(b).length, 1, "trigger fired once the drain completed");
});

function sessionParam(ws) {
  return new URL(ws.url).searchParams.get("session");
}

test("after End meeting, capture falls back to the global Session", async () => {
  const b = createBridge({ settings: { meetingSessionId: "sess-1" } });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();

  b.requestEndMeeting();
  assert.equal(triggerCalls(b).length, 1, "meeting ended + triggered");

  // A speaker talks again after the meeting: the new utterance is global.
  b.post({ kind: "tap-start", identity: "u2", name: "Bob" });
  b.post({ kind: "pcm", identity: "u2", name: "Bob", buffer: pcmFrame() });
  assert.equal(sessionParam(b.lastSocket()), null, "no session param → global Session");
});

test("End meeting keeps the stored Session id (durable for the popup card) and only marks it inactive", async () => {
  // The card re-derives progress/summary from the stored meetingSessionId
  // even after the popup closed or the Recorder restarted, so End must NOT
  // wipe it — it only flips meetingActive false so live routing falls back to
  // the global Session. The id is cleared on the next Start or a Dismiss.
  const b = createBridge({ settings: { meetingSessionId: "sess-keep" } });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();

  b.requestEndMeeting();
  await b.flushMicrotasks();

  const wrote = b.writes();
  assert.ok(
    !wrote.some((w) => "meetingSessionId" in w && w.meetingSessionId === null),
    "the durable meetingSessionId is NOT cleared on End",
  );
  const flip = wrote.find((w) => "meetingActive" in w);
  assert.ok(flip, "End wrote a meetingActive flag");
  assert.equal(flip.meetingActive, false, "meeting marked inactive (routing falls back to global)");
});

test("a 409 Session-busy response surfaces 'busy' and does not auto-hammer", async () => {
  const b = createBridge({ settings: { meetingSessionId: "sess-1" }, triggerStatus: 409 });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();

  b.requestEndMeeting();
  await b.flushMicrotasks(); // let triggerPipeline resolve + publish the outcome

  assert.equal(triggerCalls(b).length, 1, "exactly one trigger");
  b.clock.tick(60_000);
  assert.equal(triggerCalls(b).length, 1, "no auto-hammer after waiting");

  const end = b.meetingEnd();
  assert.equal(end.phase, "busy", "Recorder-busy surfaced for the popup");
  assert.equal(end.sessionId, "sess-1");
});

test("End meeting with no open taps triggers immediately on the meeting Session", async () => {
  // A meeting was started and earlier Utterances were captured, but everyone
  // is quiet now (no open taps). Ending should trigger right away.
  const b = createBridge({ settings: { meetingSessionId: "sess-1" } });
  await ready(b);

  b.requestEndMeeting();
  assert.equal(triggerCalls(b).length, 1, "trigger fires with nothing to drain");
});

test("End meeting with no active meeting is a no-op", async () => {
  const b = createBridge(); // no meeting active
  await ready(b);

  b.requestEndMeeting();
  assert.equal(triggerCalls(b).length, 0, "no trigger when no meeting is active");
});

test("a speaker muted at End meeting does not reopen a ghost tap", async () => {
  // A muted participant's audio worklet keeps emitting (silence) PCM — mute is
  // a UI/utterance-boundary signal, not a media stop. If End dropped their mute
  // state, that silence would sail past the pcm gate and open a ghost tap into
  // the global Session after the bracket is already gone.
  const b = createBridge({ settings: { meetingSessionId: "sess-1" } });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();
  b.post({ kind: "mute", identity: "u1", muted: true }); // muted before the meeting ends

  b.requestEndMeeting();
  const openedBefore = b.openSockets().length;

  // The still-muted worklet keeps delivering PCM after End.
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });

  assert.equal(
    b.openSockets().length,
    openedBefore,
    "a still-muted speaker must not reopen a tap after End",
  );
});

test("a speaker still talking (unmuted) after End is recorded into the global Session", async () => {
  // The mirror of the ghost-tap fix: preserving mute state must NOT gate a
  // genuinely-unmuted speaker who keeps talking after the meeting ends — their
  // audio still belongs in the global Session.
  const b = createBridge({ settings: { meetingSessionId: "sess-1" } });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();
  // u1 never mutes.

  b.requestEndMeeting();
  const openedBefore = b.openSockets().length;

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });

  assert.equal(
    b.openSockets().length,
    openedBefore + 1,
    "an unmuted speaker talking after End opens a fresh global tap",
  );
  assert.equal(sessionParam(b.lastSocket()), null, "routed to the global Session");
});

test("a new speaker during teardown does not stall the close-all barrier", async () => {
  // While a tap is mid-drain after End, a NEW speaker must not open a fresh
  // tap into the ending Session — that live WS would block the barrier and
  // the trigger would never fire (the meeting hangs in "Ending…").
  const b = createBridge({ settings: { meetingSessionId: "sess-1" } });
  await ready(b);

  // u1 streams, then a blip leaves buffered PCM with the WS down → drains on End.
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  ws1.triggerClose({ code: 1006, wasClean: false });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() }); // buffered

  b.requestEndMeeting();
  assert.equal(triggerCalls(b).length, 0, "u1 still draining → no trigger yet");

  // A different speaker starts talking mid-teardown — must be ignored.
  b.post({ kind: "tap-start", identity: "u2", name: "Bob" });
  b.post({ kind: "pcm", identity: "u2", name: "Bob", buffer: pcmFrame() });

  // u1's drain reconnect lands and flushes; the barrier should now complete.
  b.clock.tick(500);
  b.lastSocket().triggerOpen();

  assert.equal(triggerCalls(b).length, 1, "trigger fired; the late speaker did not stall the barrier");
});

test("a trigger error surfaces 'failed' (and still clears the meeting)", async () => {
  const b = createBridge({ settings: { meetingSessionId: "sess-1" }, triggerStatus: 500 });
  await ready(b);
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();

  b.requestEndMeeting();
  await b.flushMicrotasks(); // let triggerPipeline reject + publish the outcome

  const end = b.meetingEnd();
  assert.equal(end.phase, "failed", "a non-202/409 trigger surfaces as failed");
  assert.ok(end.error, "a human-readable failure reason is included");

  // The meeting still cleared, so capture falls back to the global Session.
  b.post({ kind: "tap-start", identity: "u2", name: "Bob" });
  b.post({ kind: "pcm", identity: "u2", name: "Bob", buffer: pcmFrame() });
  assert.equal(sessionParam(b.lastSocket()), null, "routing fell back to global after a failed trigger");
});
