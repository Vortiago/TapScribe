// New-session control-verb tests for the SpatialChat bridge.
//
// content.js gained an HTTP control verb alongside the /tap WS: on a
// `room-changed` message from page-script.js it POSTs /api/tap/new-session
// (the tap token carried as an `Authorization: Bearer` header) — but ONLY
// when the operator opted in via the popup's "start new session on room
// change" toggle. The recorder rotates to a fresh session and prunes
// empties; here we pin the bridge half of that contract.

const test = require("node:test");
const assert = require("node:assert/strict");

const { createBridge } = require("./harness");

async function ready(bridge) {
  await bridge.flushMicrotasks();
}

test("room-changed does nothing when the toggle is off", async () => {
  const b = createBridge({ settings: { autoNewSessionOnRoomChange: false } });
  await ready(b);
  b.post({ kind: "room-changed" });
  assert.equal(b.fetches().length, 0, "no new-session POST when opted out");
});

test("room-changed POSTs /api/tap/new-session with the bearer token when opted in", async () => {
  const b = createBridge({
    settings: {
      autoNewSessionOnRoomChange: true,
      recorderHost: "localhost",
      recorderPort: 9999,
      tapToken: "tok",
      useTls: false,
    },
  });
  await ready(b);
  b.post({ kind: "room-changed" });

  const calls = b.fetches();
  assert.equal(calls.length, 1, "exactly one new-session POST");
  assert.equal(calls[0].url, "http://localhost:9999/api/tap/new-session");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers.Authorization, "Bearer tok");
});

test("the room-change toggle live-reloads from storage without a tab reload", async () => {
  // Start opted out: a room-changed fires nothing.
  const b = createBridge({ settings: { autoNewSessionOnRoomChange: false } });
  await ready(b);
  b.post({ kind: "room-changed" });
  assert.equal(b.fetches().length, 0);

  // Operator ticks the box in the popup → chrome.storage.onChanged fires and
  // content.js picks the new value up live (no SpatialChat tab reload).
  b.flipAutoNewSession(true);
  b.post({ kind: "room-changed" });
  assert.equal(b.fetches().length, 1, "POST fires after the live toggle flip");
});

test("useTls sends the new-session POST over https://", async () => {
  const b = createBridge({
    settings: {
      autoNewSessionOnRoomChange: true,
      useTls: true,
      recorderHost: "rec.example",
      recorderPort: 8443,
      tapToken: "t",
    },
  });
  await ready(b);
  b.post({ kind: "room-changed" });
  const calls = b.fetches();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://rec.example:8443/api/tap/new-session");
});

test("room-changed omits the Authorization header when no tap token is set", async () => {
  // --no-auth recorders: the bridge holds no token; the POST still fires but
  // carries no bearer header (the recorder ignores it when AUTH is off).
  const b = createBridge({ settings: { autoNewSessionOnRoomChange: true, tapToken: "" } });
  await ready(b);
  b.post({ kind: "room-changed" });
  const calls = b.fetches();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.headers.Authorization, undefined);
});
