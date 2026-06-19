// Tests for popup-actions.js — the popup's DOM-free meeting side-effects.
// Inject a fake control client + fake chrome.storage and assert on the
// storage writes and the returned outcome (the behaviours the old DOM-coupled
// popup tests checked through stubs).

const test = require("node:test");
const assert = require("node:assert/strict");

let actions;
test.before(async () => { actions = await import("../popup-actions.js"); });

function fakeStorage({ reject = false } = {}) {
  const writes = [];
  return {
    writes,
    set: (items) => {
      if (reject) return Promise.reject(new Error("storage write failed"));
      writes.push(JSON.parse(JSON.stringify(items)));
      return Promise.resolve();
    },
  };
}

const CFG = { host: "localhost", port: 9999, useTls: false, token: "tok" };

test("startMeeting mints a detached session and persists the durable meeting state", async () => {
  const storage = fakeStorage();
  const control = { createDetachedSession: async () => ({ sessionId: "2026-06-19T10-00-00Z" }) };

  const out = await actions.startMeeting({ control, storage, cfg: CFG });

  assert.deepEqual(out, { ok: true, sessionId: "2026-06-19T10-00-00Z" });
  assert.equal(storage.writes.length, 1);
  assert.deepEqual(storage.writes[0], {
    meetingSessionId: "2026-06-19T10-00-00Z", meetingActive: true, meetingEnd: null,
  });
});

test("startMeeting surfaces a mint failure and persists nothing", async () => {
  const storage = fakeStorage();
  const err = Object.assign(new Error("new-session HTTP 500"), { kind: "http-error" });
  const control = { createDetachedSession: async () => { throw err; } };

  const out = await actions.startMeeting({ control, storage, cfg: CFG });

  assert.equal(out.ok, false);
  assert.equal(out.kind, "http-error");
  assert.match(out.message, /HTTP 500/);
  assert.equal(storage.writes.length, 0, "nothing persisted on a mint failure");
});

test("startMeeting reports ok:false if the mint succeeds but the persist fails", async () => {
  // The id was minted but couldn't be stored — the popup must NOT claim an
  // active meeting the content script never saw.
  const storage = fakeStorage({ reject: true });
  const control = { createDetachedSession: async () => ({ sessionId: "s" }) };

  const out = await actions.startMeeting({ control, storage, cfg: CFG });

  assert.equal(out.ok, false);
  assert.equal(out.kind, "storage");
});

test("requestEndMeeting bumps the meetingEndRequestedAt nonce", async () => {
  const storage = fakeStorage();
  await actions.requestEndMeeting({ storage, now: 1750000000000 });
  assert.deepEqual(storage.writes[0], { meetingEndRequestedAt: 1750000000000 });
});

test("dismissMeeting clears the durable meeting state", async () => {
  const storage = fakeStorage();
  await actions.dismissMeeting({ storage });
  assert.deepEqual(storage.writes[0], { meetingSessionId: null, meetingActive: false, meetingEnd: null });
});
