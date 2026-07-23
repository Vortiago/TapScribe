// Wire-contract tests for the SpatialChat bridge.
//
// What we pin here (per CONTEXT.md "Bridge" + bridges/README.md):
//   - PCM frame size is 640 bytes (20 ms @ 16 kHz mono int16). The bridge
//     forwards whatever the page-script sends; we assert that what makes
//     it onto the wire is exactly one frame per send().
//   - utterance_id is reused across reconnects within one unmuted segment,
//     so the Recorder appends to the same WAV instead of fragmenting it.
//   - A 4401 close from the Recorder (bad tap token) does NOT schedule a
//     reconnect — retrying with the same token would just thrash. The
//     operator has to fix the token via the popup; settings-change will
//     trigger a fresh dial.

const test = require("node:test");
const assert = require("node:assert/strict");

const { createBridge } = require("./harness");

function pcmFrame(byteLen = 640) {
  return new ArrayBuffer(byteLen);
}

function setupChannel(bridge, { identity = "u1", name = "Alice" } = {}) {
  bridge.post({ kind: "tap-start", identity, name });
}

async function ready(bridge) {
  await bridge.flushMicrotasks();
}

test("forwarded PCM frames are exactly 640 bytes", async () => {
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame(640) });
  const ws = b.lastSocket();
  ws.triggerOpen();

  // The bridge forwards one frame per send(). The wire contract is
  // "one 640-byte frame per WS message"; anything else would corrupt
  // WhisperLiveKit's input stream.
  assert.equal(ws.sent.length, 1, "exactly one send() per frame");
  const frame = ws.sent[0];
  // ArrayBuffer or Buffer both expose byteLength.
  assert.equal(frame.byteLength, 640, "frame length is the 20 ms wire size");
});

test("utterance_id is reused across a reconnect within one utterance", async () => {
  // CONTEXT.md invariant: one utterance = one WAV. The bridge holds the
  // utterance_id stable across a transient WS blip so the Recorder's
  // UtteranceIndex.try_resume sees the same id and appends.
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  const url1 = new URL(ws1.url);
  const utt1 = url1.searchParams.get("utterance_id");
  assert.ok(utt1, "first connection carries an utterance_id");

  // Network blip — WS closes uncleanly, the reconnect ladder fires.
  ws1.triggerClose({ code: 1006, wasClean: false });
  b.clock.tick(500);

  const ws2 = b.lastSocket();
  assert.notEqual(ws2, ws1, "reconnect opened a new WS");
  const utt2 = new URL(ws2.url).searchParams.get("utterance_id");
  assert.equal(utt2, utt1, "same utterance_id across reconnect → same WAV");
});

test("utterance_id changes after a mute → fresh-utterance cycle", async () => {
  // The flip side of the resume invariant: when a mute ends an utterance,
  // the next unmuted PCM frame must mint a fresh utterance_id. Reusing
  // the old id would make the Recorder append to a WAV that should have
  // been finalised.
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  const utt1 = new URL(ws1.url).searchParams.get("utterance_id");

  b.post({ kind: "mute", identity: "u1", muted: true });
  // The bridge processed the mute cleanly (no buffer, no drain).
  b.post({ kind: "mute", identity: "u1", muted: false });

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws2 = b.lastSocket();
  assert.notEqual(ws2, ws1, "next utterance opens a new WS");
  const utt2 = new URL(ws2.url).searchParams.get("utterance_id");
  assert.notEqual(utt2, utt1, "fresh utterance_id after mute → unmute");
});

test("4401 close does not schedule a reconnect", async () => {
  // The recorder rejects with code 4401 when the tap token is wrong.
  // Reconnecting with the same token would just thrash — wait for the
  // operator to fix the token via the popup; the storage-change handler
  // will then dial fresh.
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  const socketsBefore = b.openSockets().length;
  ws1.triggerClose({ code: 4401, reason: "missing or invalid tap token", wasClean: false });

  // Pump well past the longest backoff (5 s cap). If the bridge wrongly
  // scheduled a retry, a fresh socket would have appeared by now.
  b.clock.tick(10_000);
  const socketsAfter = b.openSockets().length;
  assert.equal(
    socketsAfter, socketsBefore,
    "no new WS after 4401 — the token must be fixed first",
  );

  const ch = b.status().channels.find((c) => c.identity === "u1");
  assert.equal(ch.error, "tap-auth-failed", "auth failure surfaces in status");
});

test("PCM frames after a 4401 do not redial with the rejected token", async () => {
  // The onclose ladder declining to retry is only HALF the guard: onclose
  // nulls ch.tapWs and arms no reconnect timer, so the pcm handler's
  // "no socket and no timer → dial" path opened a brand-new socket on the
  // very next frame — one rejected upgrade per 20 ms of speech, forever,
  // with no backoff at all. The 4401 guard test above passes only because
  // it stops posting PCM after the close; a real speaker keeps talking.
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  const ws1 = b.lastSocket();
  ws1.triggerOpen();
  ws1.triggerClose({ code: 4401, reason: "missing or invalid tap token", wasClean: false });
  const socketsBefore = b.openSockets().length;

  // Five more frames, virtual clock NEVER advanced — no backoff can be
  // credited for the quiet, so any new socket here is an immediate redial.
  for (let i = 0; i < 5; i++) {
    b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  }
  assert.equal(
    b.openSockets().length, socketsBefore,
    "tap-auth-failed is sticky — no redial while the token is still rejected",
  );

  // …and the sticky error must still be recoverable: saving a fresh token
  // clears it (reconnectAllForSettingsChange) so the next frame dials again.
  b.setTapToken("fresh-token");
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  assert.equal(
    b.openSockets().length, socketsBefore + 1,
    "a fresh token redials — the guard is sticky, not terminal",
  );
});

test("tls-required survives a gap-buffer overflow instead of degrading to recorder-unreachable", async () => {
  // MAX_BUFFER_BYTES is 96 000 B = 150 frames = 3 s of speech. Past the cap
  // bufferPush re-derives ch.error, and tls-required was not in
  // isTransportError — so the one ACTIONABLE configuration error was
  // overwritten with "recorder-unreachable", whose tooltip sends the
  // operator to the popup's Test connection. That's a dead end: the popup
  // runs on chrome-extension://, where the cleartext guard is inactive by
  // design, so it reports the recorder reachable and the token accepted
  // while no audio can ever flow. The existing tls-required test posts a
  // SINGLE frame, so it never crosses the cap.
  const b = createBridge({
    settings: { recorderHost: "macmini", recorderPort: 8001, useTls: false, tapToken: "tok" },
  });
  await ready(b);
  setupChannel(b);

  for (let i = 0; i < 200; i++) {
    b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  }
  assert.equal(b.openSockets().length, 0, "still never dialed");
  // bufferPush doesn't publish, so force the 2 Hz snapshot the popup reads —
  // otherwise this would just re-read the pre-overflow write and pass
  // vacuously.
  b.publishTick();
  const ch = b.status().channels.find((c) => c.identity === "u1");
  assert.equal(
    ch.error, "tls-required",
    "the sticky configuration diagnosis outranks the derived overflow label",
  );
});

test("ws:// to a non-trustworthy host from https:// is refused with tls-required, not silently thrown", async () => {
  // Chrome/Edge block `new WebSocket("ws://macmini:8001/...")` from an
  // https:// page as mixed content — the constructor throws
  // SecurityError synchronously. Before the guard, that throw escaped
  // the PCM handler on every frame and left /tap stuck at "idle" with
  // no clue why. The bridge must now:
  //   1. Detect this configuration up front and not even dial.
  //   2. Surface "tls-required" so the popup pill turns red.
  //   3. NOT construct a FakeWebSocket (the real one would have thrown).
  const b = createBridge({
    settings: { recorderHost: "macmini", recorderPort: 8001, useTls: false, tapToken: "tok" },
  });
  await ready(b);
  setupChannel(b);

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  assert.equal(b.openSockets().length, 0, "no WS attempted when mixed-content-blocked");

  const ch = b.status().channels.find((c) => c.identity === "u1");
  assert.equal(ch.error, "tls-required", "tls-required surfaces in status");
  assert.equal(ch.tapWs, null, "tapWs stays null so popup pill stays 'idle'");
});

test("localhost is exempt from the mixed-content guard", async () => {
  // Loopback (localhost, 127.0.0.1, ::1) is a "potentially trustworthy
  // origin" — Chrome and Edge both allow ws:// to it even from https://.
  // The bridge mustn't slap tls-required on dev setups against a local
  // recorder.
  for (const host of ["localhost", "127.0.0.1", "::1", "foo.localhost"]) {
    const b = createBridge({ settings: { recorderHost: host, useTls: false } });
    await ready(b);
    setupChannel(b);
    b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
    assert.equal(
      b.openSockets().length, 1,
      "WS dialed against trustworthy host " + host,
    );
    const ch = b.status().channels.find((c) => c.identity === "u1");
    assert.equal(ch.error, null, "no tls-required against " + host);
  }
});

test("wss:// to a remote host is exempt from the mixed-content guard", async () => {
  // The guard targets ws://-from-https:// specifically. wss:// is fine
  // from https:// — the operator who turned on --tls on a remote
  // recorder is exactly the case we want to unblock.
  const b = createBridge({
    settings: { recorderHost: "macmini", recorderPort: 8001, useTls: true, tapToken: "tok" },
  });
  await ready(b);
  setupChannel(b);
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  assert.equal(b.openSockets().length, 1, "wss:// dialed without complaint");
  assert.ok(b.lastSocket().url.startsWith("wss://"), "scheme is wss");
});

test("flipping Use TLS on clears the sticky tls-required error so the bridge redials", async () => {
  // The mixed-content guard sets ch.error = "tls-required" and skips
  // the dial. When the operator fixes the config (ticks Use TLS in the
  // popup) the storage-change handler should drop the sticky error so
  // the next PCM frame redials against the new scheme — same recovery
  // pattern as fixing a 4401 tap token.
  const b = createBridge({
    settings: { recorderHost: "macmini", recorderPort: 8001, useTls: false, tapToken: "tok" },
  });
  await ready(b);
  setupChannel(b);
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  assert.equal(b.openSockets().length, 0);
  assert.equal(
    b.status().channels.find((c) => c.identity === "u1").error,
    "tls-required",
  );

  // Operator ticks "Use TLS" in the popup. chrome.storage.onChanged
  // fires; the bridge clears the sticky error and the next PCM frame
  // should redial — this time over wss://.
  b.flipUseTls(true);
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  assert.equal(b.openSockets().length, 1, "redial after settings change");
  assert.ok(b.lastSocket().url.startsWith("wss://"));
});

// Wraps client.wouldBlockCleartext with a call-recording spy that still
// delegates to the real predicate, so behavior is unaffected — only used to
// pin WHICH function the ws:// guard consults, not its blocked/allowed
// outcome (already covered by the guard tests above). Scope caveat: the spy
// observes content.js's property-path call ONLY. control-client.js's own HTTP
// guard (controlFetch) invokes wouldBlockCleartext through a private closure
// binding inside the IIFE, which no property patch can see — the HTTP side is
// pinned behaviorally in the test below, not by this spy.
function spyOnWouldBlockCleartext(bridge) {
  const client = bridge.controlClient();
  const calls = [];
  const real = client.wouldBlockCleartext;
  client.wouldBlockCleartext = (cfg) => {
    calls.push(cfg);
    return real(cfg);
  };
  return calls;
}

test("the WS pre-flight shares the exported wouldBlockCleartext, and the HTTP guard blocks the same cfg", async () => {
  // #251 shared the inner isTrustworthyHost; content.js still hand-rolled
  // the surrounding three-step cleartext predicate (TLS off + https page +
  // non-trustworthy host) as its own wouldBeMixedContentBlocked. Two pins:
  //   1. Identity, WS side: the spy proves content.js's ws:// pre-flight
  //      calls through the exported TapscribeControlClient.wouldBlockCleartext
  //      rather than keeping a private duplicate predicate.
  //   2. Behavior, HTTP side: the spy CANNOT prove the same for the HTTP
  //      control plane — controlFetch reaches the predicate via a closure
  //      binding a property patch can't observe — so pin its observable
  //      contract instead: the SAME blocked cfg makes a control call reject
  //      as mixed-content-blocked carrying the shared display copy, with
  //      nothing reaching fetch. A fork of the HTTP guard that diverges from
  //      the WS verdict on this cfg trips this half.
  const blocked = createBridge({
    settings: { recorderHost: "macmini", recorderPort: 8001, useTls: false, tapToken: "tok" },
  });
  await ready(blocked);
  setupChannel(blocked);
  const blockedCalls = spyOnWouldBlockCleartext(blocked);

  // (1) WS side — predicate identity via the spy.
  blocked.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  assert.ok(
    blockedCalls.some((cfg) => cfg && cfg.host === "macmini"),
    "the ws:// guard consulted TapscribeControlClient.wouldBlockCleartext with the recorder cfg",
  );

  // (2) HTTP side — observable blocking behavior under the same cfg. The
  // error is built inside the vm realm, so validate fields, not instanceof.
  const client = blocked.controlClient();
  await assert.rejects(
    client.triggerPipeline({ host: "macmini", port: 8001, useTls: false, token: "tok" }, "sess-http"),
    (e) => e.kind === "mixed-content-blocked" && e.message === client.MIXED_CONTENT_BLOCKED_TEXT,
    "the HTTP control plane must block the same cfg with the shared display copy",
  );
  assert.equal(blocked.fetches().length, 0, "the blocked control call never reached fetch");
});

test("forwarded frame body is the same bytes the page-script sent", async () => {
  // Sanity-check: the bridge doesn't mutate / reframe / pad PCM. Whatever
  // page-script.js posts goes through verbatim. A regression that wrapped
  // the buffer (e.g. with a Blob, JSON, header bytes) would silently
  // corrupt WlK's stream.
  const b = createBridge();
  await ready(b);
  setupChannel(b);

  const buf = new ArrayBuffer(640);
  const view = new Uint8Array(buf);
  for (let i = 0; i < view.length; i++) view[i] = i & 0xff;

  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: buf });
  const ws = b.lastSocket();
  ws.triggerOpen();
  assert.equal(ws.sent.length, 1);
  const sent = ws.sent[0];
  const sentView = new Uint8Array(sent);
  for (let i = 0; i < sentView.length; i++) {
    assert.equal(sentView[i], i & 0xff, "byte " + i + " preserved");
  }
});

test("a capture-failed signal from the page world lands on the speaker's channel", async () => {
  // page-script.js posts this when a tapped MediaStreamTrack fires `ended`
  // (mic unplugged, permission revoked, device switched away). It arrives
  // immediately before the tap-stop that closes the Utterance, so the reason
  // has to be recorded while the channel is still live — a tombstoned
  // channel is filtered out of every operator-facing list.
  const b = createBridge();
  await ready(b);
  setupChannel(b);
  b.post({ kind: "pcm", identity: "u1", name: "Alice", buffer: pcmFrame() });
  b.lastSocket().triggerOpen();

  b.post({ kind: "capture-failed", identity: "u1", reason: "track-ended" });
  const ch = b.status().channels.find((c) => c.identity === "u1");
  assert.equal(ch.error, "capture-track-ended", "the popup row carries WHY capture stopped");

  // A rebind (reconcile taps the replacement track) re-arms the channel and
  // clears the error — a channel with a fresh live track is not failed.
  b.post({ kind: "tap-stop", identity: "u1" });
  b.post({ kind: "tap-start", identity: "u1", name: "Alice" });
  const rearmed = b.status().channels.find((c) => c.identity === "u1");
  assert.equal(rearmed.error, null, "a successful rebind clears the capture failure");
});
