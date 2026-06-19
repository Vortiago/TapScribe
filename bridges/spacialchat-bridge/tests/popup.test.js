// Tests for popup.js — the extension popup. Focus: the "Start meeting"
// button (mint a DETACHED session via the shared control-client, persist
// meetingSessionId to chrome.storage for the content script to route taps
// into, disable while a meeting is active). popup.js is a flat script (no
// exports) that calls load() and wires listeners at load time, so — like
// page-script.test.js — we load it into a vm with a mocked browser/extension
// environment and drive the registered handlers.
//
// The on-load path runs a /health fetch + a /tap WebSocket token probe; both
// are mocked to resolve cleanly so load() settles without network.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const CONTROL_CLIENT_JS = path.join(__dirname, "..", "control-client.js");
const POPUP_JS = path.join(__dirname, "..", "popup.js");

function makeEl(id) {
  const listeners = {};
  return {
    id,
    value: "",
    checked: false,
    textContent: "",
    className: "",
    innerHTML: "",
    addEventListener: (ev, fn) => {
      (listeners[ev] || (listeners[ev] = [])).push(fn);
    },
    _fire: (ev, arg) => {
      for (const fn of listeners[ev] || []) fn(arg || {});
    },
  };
}

function createPopup({ settings = {}, post, setRejects = false } = {}) {
  const store = {
    recorderHost: "localhost",
    recorderPort: 8001,
    tapToken: "tok",
    useTls: false,
    ...settings,
  };
  // Default POST (new-session) response; overridable per test.
  const postResp = post || {
    ok: true,
    status: 200,
    body: { ok: true, rotated: true, current: "2026-06-02T12-00-00Z" },
  };

  const els = {};
  const fetchCalls = [];
  const tabsCreated = [];
  const storageWrites = [];
  const changeListeners = [];

  // Fake WebSocket for the on-load tap-token probe: auto-open on the next
  // microtask so probeTapToken() resolves ok and load() settles.
  class FakeWS {
    constructor(url, protocols) {
      this.url = url;
      this.protocols = protocols;
      this.onopen = null;
      this.onerror = null;
      this.onclose = null;
      Promise.resolve().then(() => {
        if (this.onopen) this.onopen({});
      });
    }
    close() {}
  }

  const sandbox = {
    document: {
      getElementById: (id) => els[id] || (els[id] = makeEl(id)),
    },
    chrome: {
      storage: {
        local: {
          get: (keys) =>
            Promise.resolve(
              Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map((k) => [k, store[k]])),
            ),
          set: (obj) => {
            // Model a storage write that rejects (quota, profile error): the
            // store is NOT mutated and no write is recorded, as the browser
            // would do on a failed set.
            if (setRejects) return Promise.reject(new Error("storage write failed"));
            Object.assign(store, obj);
            storageWrites.push(JSON.parse(JSON.stringify(obj)));
            return Promise.resolve();
          },
        },
        onChanged: { addListener: (fn) => changeListeners.push(fn) },
      },
      tabs: { create: (o) => tabsCreated.push(o) },
    },
    fetch: (url, opts) => {
      const method = (opts && opts.method) || "GET";
      fetchCalls.push({ url, method, options: opts || {} });
      if (method === "POST") {
        return Promise.resolve({
          ok: postResp.ok,
          status: postResp.status,
          json: () => Promise.resolve(postResp.body),
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ status: "ok" }) });
    },
    WebSocket: FakeWS,
    AbortController: class {
      constructor() {
        this.signal = { addEventListener() {} };
      }
      abort() {}
    },
    URLSearchParams,
    setTimeout: () => 0, // don't let the 4s abort timer fire under test
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    window: { addEventListener: () => {} },
    console: { log() {}, warn() {}, error() {} },
  };
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  // Loaded ahead of popup.js exactly as popup.html orders the <script>
  // tags — it defines the TapscribeControlClient global popup.js calls.
  vm.runInContext(fs.readFileSync(CONTROL_CLIENT_JS, "utf8"), sandbox, { filename: "control-client.js" });
  vm.runInContext(fs.readFileSync(POPUP_JS, "utf8"), sandbox, { filename: "popup.js" });

  // Drain the load() → refresh() → probeAll() promise chain.
  async function settle() {
    for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
  }

  return {
    els,
    fetchCalls,
    storageWrites,
    tabsCreated,
    settle,
    postCalls: () => fetchCalls.filter((c) => c.method === "POST"),
    el: (id) => els[id] || (els[id] = makeEl(id)),
    click: (id) => (els[id] || makeEl(id))._fire("click", { preventDefault() {} }),
    change: (id) => (els[id] || makeEl(id))._fire("change"),
    // Fire the popup's chrome.storage.onChanged listeners — models the
    // content script (or another popup) updating storage while open.
    fireChange: (changes) => {
      for (const fn of changeListeners) fn(changes, "local");
    },
  };
}

// ---- Start meeting (bracketed detached Session) ---------------------------

const DETACHED_OK = {
  ok: true,
  status: 200,
  body: { ok: true, detached: true, session: "2026-06-19T10-00-00Z", path: "recordings/2026-06-19T10-00-00Z" },
};

test("Start meeting mints a detached Session and persists meetingSessionId to storage", async () => {
  const p = createPopup({
    settings: { recorderHost: "localhost", recorderPort: 9999, tapToken: "tok" },
    post: DETACHED_OK,
  });
  await p.settle();

  p.click("startMeeting");
  await p.settle();

  const posts = p.postCalls();
  assert.equal(posts.length, 1, "exactly one new-session POST");
  assert.equal(posts[0].url, "http://localhost:9999/api/tap/new-session");
  assert.equal(posts[0].options.headers.Authorization, "Bearer tok");
  assert.equal(JSON.parse(posts[0].options.body).detached, true, "asks for a DETACHED session");

  const write = p.storageWrites.find((w) => "meetingSessionId" in w);
  assert.ok(write, "meetingSessionId was persisted for the content script to pick up");
  assert.equal(write.meetingSessionId, "2026-06-19T10-00-00Z");
});

test("Start meeting is disabled after a successful start (can't orphan a second meeting)", async () => {
  const p = createPopup({ post: DETACHED_OK });
  await p.settle();
  assert.equal(p.el("startMeeting").disabled, false, "enabled before any meeting");

  p.click("startMeeting");
  await p.settle();
  assert.equal(p.el("startMeeting").disabled, true, "disabled while a meeting is active");
});

test("Start meeting is disabled on load when a meeting is already active", async () => {
  // The popup is ephemeral — re-opening it must reflect the durable state.
  const p = createPopup({ settings: { meetingSessionId: "2026-06-19T09-00-00Z" } });
  await p.settle();
  assert.equal(p.el("startMeeting").disabled, true, "disabled because a meeting is stored");
  assert.match(p.el("meetingStatus").textContent, /Meeting active/);
  assert.match(p.el("meetingStatus").textContent, /2026-06-19T09-00-00Z/);
});

test("a failed start surfaces an error, persists nothing, and re-enables the button", async () => {
  const p = createPopup({ post: { ok: false, status: 500, body: {} } });
  await p.settle();

  p.click("startMeeting");
  await p.settle();

  assert.match(p.el("meetingStatus").textContent, /Start meeting failed/);
  assert.equal(p.el("startMeeting").disabled, false, "re-enabled so the operator can retry");
  assert.ok(
    !p.storageWrites.some((w) => "meetingSessionId" in w),
    "no meetingSessionId persisted on failure",
  );
});

test("a failed persist does not show a false 'Meeting active' state", async () => {
  // createDetachedSession succeeds but the chrome.storage write rejects.
  // The stored id is the real "meeting started" signal (it's what the
  // content script routes on), so the popup must NOT claim the meeting is
  // active when nothing persisted — the button stays usable.
  const p = createPopup({ post: DETACHED_OK, setRejects: true });
  await p.settle();

  p.click("startMeeting");
  await p.settle();

  assert.doesNotMatch(p.el("meetingStatus").textContent, /Meeting active/, "no false success");
  assert.match(p.el("meetingStatus").textContent, /failed/i);
  assert.equal(p.el("startMeeting").disabled, false, "button left usable for retry");
});

// ---- End meeting -----------------------------------------------------------

test("End meeting requests the end via storage and shows 'Ending…'", async () => {
  const p = createPopup({ settings: { meetingSessionId: "sess-1" } });
  await p.settle();

  assert.equal(p.el("endMeeting").disabled, false, "End is enabled while a meeting is active");

  p.click("endMeeting");
  await p.settle();

  const write = p.storageWrites.find((w) => "meetingEndRequestedAt" in w);
  assert.ok(write, "End wrote a meetingEndRequestedAt request for the content script");
  assert.match(p.el("meetingStatus").textContent, /Ending/i);
});

test("with no meeting active, Start is enabled and End is disabled", async () => {
  const p = createPopup(); // no meeting
  await p.settle();
  assert.equal(p.el("startMeeting").disabled, false, "Start enabled");
  assert.equal(p.el("endMeeting").disabled, true, "End disabled — nothing to end");
});

test("a 'busy' end outcome surfaces a clear 'Recorder busy' state", async () => {
  const p = createPopup({ settings: { meetingSessionId: "sess-1" } });
  await p.settle();

  // The content script published a busy outcome (409) to storage.
  p.fireChange({ meetingEnd: { newValue: { phase: "busy", sessionId: "sess-1" } } });

  assert.match(p.el("meetingStatus").textContent, /busy/i, "Recorder-busy surfaced");
});

test("clearing meetingSessionId externally re-enables Start (popup live-refreshes)", async () => {
  const p = createPopup({ settings: { meetingSessionId: "sess-1" } });
  await p.settle();
  assert.equal(p.el("startMeeting").disabled, true, "Start disabled while active");

  // The content script cleared the meeting after End completed.
  p.fireChange({ meetingSessionId: { newValue: null } });

  assert.equal(p.el("startMeeting").disabled, false, "Start re-enabled once the meeting cleared");
  assert.equal(p.el("endMeeting").disabled, true, "End disabled once the meeting cleared");
});
