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
