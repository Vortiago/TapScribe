// Tests for popup.js — the extension popup. Focus: the "New session" button
// (POST /api/tap/new-session with the tap-token bearer header + status
// labels) and the "start new session on room change" toggle persisting to
// chrome.storage. popup.js is a flat script (no exports) that calls load()
// and wires listeners at load time, so — like page-script.test.js — we load
// it into a vm with a mocked browser/extension environment and drive the
// registered handlers.
//
// The on-load path runs a /health fetch + a /tap WebSocket token probe; both
// are mocked to resolve cleanly so load() settles without network.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

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

function createPopup({ settings = {}, post } = {}) {
  const store = {
    recorderHost: "localhost",
    recorderPort: 8001,
    tapToken: "tok",
    useTls: false,
    autoNewSessionOnRoomChange: false,
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
  };
}

test("New session button POSTs /api/tap/new-session with the bearer token and shows the id", async () => {
  const p = createPopup({ settings: { recorderHost: "localhost", recorderPort: 9999, tapToken: "tok" } });
  await p.settle();

  p.click("newSession");
  await p.settle();

  const posts = p.postCalls();
  assert.equal(posts.length, 1, "exactly one new-session POST");
  assert.equal(posts[0].url, "http://localhost:9999/api/tap/new-session");
  assert.equal(posts[0].options.headers.Authorization, "Bearer tok");
  assert.match(p.el("newSessionStatus").textContent, /New session started/);
  assert.match(p.el("newSessionStatus").textContent, /2026-06-02T12-00-00Z/);
});

test("rotated:false response shows the 'already fresh' label", async () => {
  const p = createPopup({ post: { ok: true, status: 200, body: { ok: true, rotated: false } } });
  await p.settle();
  p.click("newSession");
  await p.settle();
  assert.match(p.el("newSessionStatus").textContent, /Already on a fresh session/);
});

test("a non-ok response shows a failure status", async () => {
  const p = createPopup({ post: { ok: false, status: 500, body: {} } });
  await p.settle();
  p.click("newSession");
  await p.settle();
  assert.match(p.el("newSessionStatus").textContent, /New session failed \(HTTP 500\)/);
});

test("useTls sends the POST over https://", async () => {
  const p = createPopup({
    settings: { useTls: true, recorderHost: "rec.example", recorderPort: 8443, tapToken: "t" },
  });
  await p.settle();
  p.click("newSession");
  await p.settle();
  assert.equal(p.postCalls()[0].url, "https://rec.example:8443/api/tap/new-session");
});

test("no tap token omits the Authorization header", async () => {
  const p = createPopup({ settings: { tapToken: "" } });
  await p.settle();
  p.click("newSession");
  await p.settle();
  assert.equal(p.postCalls()[0].options.headers.Authorization, undefined);
});

test("toggling 'new session on room change' persists immediately to chrome.storage", async () => {
  const p = createPopup({ settings: { autoNewSessionOnRoomChange: false } });
  await p.settle();

  p.el("autoNewSessionOnRoomChange").checked = true;
  p.change("autoNewSessionOnRoomChange");

  const write = p.storageWrites.find((w) => "autoNewSessionOnRoomChange" in w);
  assert.ok(write, "a storage write for the toggle was made");
  assert.equal(write.autoNewSessionOnRoomChange, true);
});
