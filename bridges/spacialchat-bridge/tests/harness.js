// Test harness for content.js
//
// content.js is the bridge's content-script IIFE that runs in a Chrome
// extension's isolated world. It assumes a browser environment (window,
// document, chrome.*, WebSocket, crypto, console, setTimeout). To make
// the IIFE testable from Node we build a fake browser global object,
// load the script into a `vm` context, and expose hooks for tests to
// drive postMessage events and inspect side effects.
//
// What the harness gives each test:
//   - `post(msg)`         — synthesise a window.postMessage from the
//                           page-world page-script.js
//   - `openSockets()`     — list of FakeWebSocket instances ever created
//   - `lastSocket()`      — most recently constructed FakeWebSocket
//   - `status()`          — last bridgeStatus snapshot written to
//                           chrome.storage.local (the popup's view)
//   - `clock.tick(ms)`    — advance virtual time and fire any timers
//                           whose deadlines have arrived
//   - `publishTick()`     — fire the 2 Hz publish/title interval body once
//                           (not auto-fired; drives the tab-title suffix)
//   - `title()`           — the current document.title the script maintains
//
// We deliberately do NOT use real timers: every test would have to
// `await new Promise(r => setTimeout(r, ...))` and the drain timeout
// alone is 8s. A virtual clock makes the suite deterministic and fast.

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const CONTROL_CLIENT_JS = path.join(__dirname, "..", "control-client.js");
const CONTENT_JS = path.join(__dirname, "..", "content.js");

function createClock() {
  let now = 0;
  let nextId = 1;
  const timers = new Map(); // id -> { at, fn }

  function setTimeoutFn(fn, ms) {
    const id = nextId++;
    timers.set(id, { at: now + (ms || 0), fn });
    return id;
  }
  function clearTimeoutFn(id) {
    timers.delete(id);
  }
  function tick(ms) {
    const target = now + ms;
    while (true) {
      let due = null;
      let dueAt = Infinity;
      for (const [id, t] of timers) {
        if (t.at <= target && t.at < dueAt) {
          due = id;
          dueAt = t.at;
        }
      }
      if (due === null) break;
      const t = timers.get(due);
      timers.delete(due);
      now = t.at;
      try { t.fn(); } catch (e) { /* surface in test if needed */ }
    }
    now = target;
  }
  return {
    setTimeout: setTimeoutFn,
    clearTimeout: clearTimeoutFn,
    tick,
    get now() { return now; },
    pending: () => timers.size,
  };
}

class FakeWebSocket {
  constructor(url, protocols) {
    this.url = url;
    this.protocols = protocols;
    this.readyState = 0; // CONNECTING
    this.binaryType = "blob";
    this.bufferedAmount = 0;
    this.sent = []; // ArrayBuffer or Buffer of every send()
    this.closed = null; // { code, reason } or null
    this.onopen = null;
    this.onclose = null;
    this.onerror = null;
    this.onmessage = null;
    FakeWebSocket._all.push(this);
  }
  // Static state shared across the test
  static reset() { FakeWebSocket._all = []; }
  // Drive lifecycle from the test
  triggerOpen() {
    this.readyState = 1;
    if (this.onopen) this.onopen({});
  }
  triggerClose({ code = 1006, reason = "", wasClean = false } = {}) {
    this.readyState = 3;
    this.closed = { code, reason, wasClean };
    if (this.onclose) this.onclose({ code, reason, wasClean });
  }
  triggerError() {
    if (this.onerror) this.onerror({});
  }
  send(data) {
    if (this.readyState !== 1) throw new Error("send on non-open WS");
    this.sent.push(data);
  }
  close(code, reason) {
    // Mirror real-browser semantics: readyState -> CLOSING, then the
    // close callback fires once the FIN lands. Most tests don't care
    // about the intermediate state; they call triggerClose() to drive
    // onclose explicitly.
    if (this.readyState === 1 || this.readyState === 0) {
      this.readyState = 2;
      this.closed = { code: code || 1000, reason: reason || "", wasClean: true };
      if (this.onclose) {
        this.readyState = 3;
        this.onclose({ code: code || 1000, reason: reason || "", wasClean: true });
      }
    }
  }
}
FakeWebSocket._all = [];
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;

function makeChromeMock(initialSettings) {
  const store = { ...initialSettings };
  const writes = []; // { key: value } snapshots
  const changeListeners = [];
  return {
    runtime: { getURL: (p) => "chrome-extension://test/" + p },
    storage: {
      local: {
        get: (keys) => Promise.resolve(
          Object.fromEntries(keys.map((k) => [k, store[k]])),
        ),
        set: (obj) => {
          Object.assign(store, obj);
          writes.push(JSON.parse(JSON.stringify(obj)));
          return Promise.resolve();
        },
      },
      onChanged: {
        addListener: (fn) => changeListeners.push(fn),
      },
    },
    _writes: writes,
    _fireChange: (changes) => {
      for (const fn of changeListeners) {
        try { fn(changes, "local"); } catch (e) { /* surface via assert */ }
      }
    },
  };
}

function createBridge({ settings = {}, location: locationOverride, triggerStatus = 202 } = {}) {
  FakeWebSocket.reset();
  const clock = createClock();
  // The content script's 2 Hz publish/title interval callback(s), captured at
  // registration so a test can fire the tick on demand via `publishTick()`.
  const intervalCbs = [];
  // Records every fetch() the content script makes (the new-session POST).
  const fetchCalls = [];
  const chrome = makeChromeMock({
    // Default to localhost so the mixed-content guard (ws:// from
    // https:// blocked unless host is trustworthy) doesn't fire in the
    // wire-contract tests. Tests for the guard itself override this.
    recorderHost: "localhost",
    recorderPort: 9999,
    tapToken: "tok",
    useTls: false,
    ...settings,
  });
  // Capture the message handler the content script installs so the test
  // can dispatch page-world events synchronously.
  let messageHandler = null;
  const fakeWindow = {
    __tapscribeBridgeContentInstalled: undefined,
    addEventListener: (ev, fn) => {
      if (ev === "message") messageHandler = fn;
    },
  };
  // Minimal element stub good enough for the indicator code path:
  // accepts arbitrary property writes, has a `style` object so
  // `el.style.cssText = "…"` works, tracks children for `contains()`,
  // and `attachShadow()` returns another stub so the indicator's
  // shadow-root scoping doesn't blow up under the harness.
  function makeNode() {
    const kids = [];
    const node = {
      style: {},
      _kids: kids,
      appendChild(child) { kids.push(child); return child; },
      contains(child) {
        if (kids.includes(child)) return true;
        for (const k of kids) {
          if (k && typeof k.contains === "function" && k.contains(child)) return true;
        }
        return false;
      },
      remove() {},
      setAttribute() {},
      addEventListener() {},
      attachShadow() {
        // Real Shadow DOM hangs the root off the host but it isn't a
        // regular child. The harness only needs a place to find it, so
        // stash it on `_shadow` and let the indicator() helper look there.
        const shadow = makeNode();
        node._shadow = shadow;
        return shadow;
      },
    };
    return node;
  }
  const sandbox = {
    window: fakeWindow,
    document: {
      title: "test",
      head: makeNode(),
      documentElement: makeNode(),
      createElement: () => makeNode(),
    },
    // The bridge runs inside the SpatialChat tab, which is always
    // https://. The mixed-content guard checks `location.protocol`
    // and `location.origin`, so populate both. Tests can override
    // via `createBridge({ location: { ... } })` to simulate other
    // origins (e.g. a localhost dev page that allows ws://).
    location: locationOverride || {
      href: "https://app.spatial.chat/test",
      protocol: "https:",
      origin: "https://app.spatial.chat",
    },
    chrome,
    WebSocket: FakeWebSocket,
    fetch: (url, options) => {
      fetchCalls.push({ url, options: options || {} });
      // The only control call the content script makes is the end-of-meeting
      // pipeline trigger; default to 202 (accepted). Tests override
      // triggerStatus to exercise 409 (busy) / other failures.
      return Promise.resolve({
        ok: triggerStatus >= 200 && triggerStatus < 300,
        status: triggerStatus,
        json: () => Promise.resolve({}),
      });
    },
    crypto: { randomUUID: () => "u-" + Math.random().toString(36).slice(2) },
    URLSearchParams,
    AbortController,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    // The 2 Hz publishStatus/title tick is NOT auto-fired — tests drive state
    // explicitly via post() for determinism. We capture the callback (rather
    // than discard it) so a test that specifically needs the tick's side
    // effects (the tab-title suffix is built ONLY here) can run it on demand
    // via `publishTick()`. Capturing-but-not-firing keeps every other test
    // unchanged: the body never runs unless a test asks for it.
    setInterval: (fn) => { if (typeof fn === "function") intervalCbs.push(fn); return intervalCbs.length; },
    clearInterval: () => {},
    console: { log: () => {}, warn: () => {}, error: () => {} },
  };
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  // Mirror the manifest's load order: control-client.js defines the shared
  // global the content script's control calls go through, and must run
  // before content.js. Re-read and re-executed fresh into this bridge's own
  // sandbox on every createBridge() call, so the resulting
  // TapscribeControlClient object is never shared across bridges — a test
  // that monkey-patches one of its members via controlClient() (see below)
  // can't leak that patch into another bridge or a later test.
  vm.runInContext(fs.readFileSync(CONTROL_CLIENT_JS, "utf8"), sandbox, { filename: "control-client.js" });
  const code = fs.readFileSync(CONTENT_JS, "utf8");
  vm.runInContext(code, sandbox, { filename: "content.js" });

  // The content script reads settings asynchronously via a Promise. In
  // Node we need to flush microtasks before the script will accept PCM
  // (it gates on `settingsReady`). A single drain of pending promises
  // is enough — the .then() handler runs and flips the flag.
  function flushMicrotasks() {
    return new Promise((resolve) => setImmediate(resolve));
  }

  function post(msg) {
    if (!messageHandler) throw new Error("no message handler installed");
    messageHandler({
      source: fakeWindow,
      data: { source: "tapscribe-bridge", ...msg },
    });
  }

  function status() {
    if (chrome._writes.length === 0) return null;
    for (let i = chrome._writes.length - 1; i >= 0; i--) {
      if (chrome._writes[i].bridgeStatus) return chrome._writes[i].bridgeStatus;
    }
    return null;
  }

  // The latest `meetingEnd` outcome the content script published to storage
  // (the popup's view of the End-meeting result), or null if none yet.
  function meetingEnd() {
    for (let i = chrome._writes.length - 1; i >= 0; i--) {
      if (chrome._writes[i].meetingEnd) return chrome._writes[i].meetingEnd;
    }
    return null;
  }

  // Simulate the operator changing settings in the popup — fires the
  // chrome.storage.onChanged listener the content script registered at
  // boot. Matches the real shape: `{ key: { newValue, oldValue } }`.
  function flipUseTls(useTls) {
    chrome._fireChange({ useTls: { newValue: !!useTls, oldValue: !useTls } });
  }

  // Simulate the popup starting / ending a bracketed meeting — fires the
  // same onChanged listener content.js registered at boot, mirroring the
  // popup persisting (or clearing) meetingSessionId in chrome.storage.local.
  function startMeeting(sessionId) {
    chrome._fireChange({
      meetingSessionId: { newValue: sessionId, oldValue: null },
    });
  }
  function endMeeting() {
    chrome._fireChange({
      meetingSessionId: { newValue: null, oldValue: "stale" },
    });
  }

  // Simulate the popup's "End meeting" button: bump the meetingEndRequestedAt
  // nonce in storage, which the content script's onChanged listener turns
  // into the drain → close-all → trigger sequence. A monotonic nonce so a
  // second End fires onChanged again.
  let endNonce = 0;
  function requestEndMeeting() {
    const prev = endNonce;
    endNonce += 1;
    chrome._fireChange({
      meetingEndRequestedAt: { newValue: endNonce, oldValue: prev },
    });
  }

  // Simulate the popup RESETTING the nonce (startMeeting/dismissMeeting write
  // meetingEndRequestedAt: null so a stale request can't haunt the next
  // meeting) — a change event whose newValue is not a number.
  function resetEndRequest() {
    chrome._fireChange({
      meetingEndRequestedAt: { newValue: null, oldValue: endNonce },
    });
  }

  // The in-page status pill lives in a shadow root attached to a host
  // element appended to documentElement. The harness mocks documentElement
  // and createElement above, so we can find the host by its id and dig
  // into its (mocked) shadow tree to read the current pill class / label.
  function indicator() {
    const root = sandbox.document.documentElement;
    if (!root || !root._kids) return null;
    const host = root._kids.find((k) => k && k.id === "__tapscribe_indicator_host__");
    if (!host) return null;
    const shadow = host._shadow || host;
    if (!shadow || !shadow._kids) return { host, kind: null, text: null, title: host.title || null };
    // shadow._kids = [<style>, <pill>]; pill._kids = [<dot>, <label>]
    const pill = shadow._kids && shadow._kids.find((k) => k && typeof k.className === "string" && k.className.indexOf("pill") === 0);
    const label = pill && pill._kids && pill._kids.find((k) => k && k.className === "label");
    const cls = pill ? pill.className : null;
    const kind = cls ? cls.replace(/^pill\s+/, "") : null;
    return {
      host,
      kind,
      text: label ? label.textContent : null,
      title: host.title || null,
    };
  }

  // Simulate SpatialChat's SPA router wiping documentElement's children
  // (route change replaces the tree). Used by indicator re-mount tests
  // to verify the bridge re-appends its host on the next state change.
  function detachIndicator() {
    const root = sandbox.document.documentElement;
    if (root && Array.isArray(root._kids)) {
      const idx = root._kids.findIndex((k) => k && k.id === "__tapscribe_indicator_host__");
      if (idx >= 0) root._kids.splice(idx, 1);
      return idx >= 0;
    }
    return false;
  }

  return {
    post,
    status,
    meetingEnd,
    // The sandbox's TapscribeControlClient global, live — control-client.js
    // assigns it before content.js loads, and content.js looks the name up
    // fresh on every call rather than capturing a reference at load time. A
    // test can therefore wrap one of its members with a spy AFTER the bridge
    // is constructed and still observe content.js calling through it (e.g.
    // pinning that content.js's ws:// pre-flight consults the exported
    // wouldBlockCleartext). Scope caveat: such a spy sees only property-path
    // callers like content.js — control-client.js's own internals reach
    // sibling functions via closure bindings a property patch can't observe,
    // so the HTTP guard must be pinned behaviorally, not by spying.
    controlClient: () => sandbox.TapscribeControlClient,
    // Every chrome.storage.local.set the content script made, in order — lets
    // a test assert on the durable meeting state (id kept, meetingActive
    // flipped) the popup card re-reads.
    writes: () => chrome._writes,
    openSockets: () => FakeWebSocket._all,
    lastSocket: () => FakeWebSocket._all[FakeWebSocket._all.length - 1],
    fetches: () => fetchCalls,
    clock,
    flushMicrotasks,
    flipUseTls,
    startMeeting,
    endMeeting,
    requestEndMeeting,
    resetEndRequest,
    indicator,
    detachIndicator,
    // Fire the content script's interval tick(s) on demand (the harness never
    // auto-fires them). content.js registers one 2 Hz publish/title interval
    // today and the tab-title suffix is built only inside it, so a title
    // assertion must call this first; firing every registered body keeps this
    // correct if a second interval is ever added.
    publishTick: () => { for (const fn of intervalCbs) fn(); },
    // The current tab title (document.title) the content script maintains.
    title: () => sandbox.document.title,
  };
}

module.exports = { createBridge, FakeWebSocket };
