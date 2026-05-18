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
//
// We deliberately do NOT use real timers: every test would have to
// `await new Promise(r => setTimeout(r, ...))` and the drain timeout
// alone is 8s. A virtual clock makes the suite deterministic and fast.

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

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

function createBridge({ settings = {}, location: locationOverride } = {}) {
  FakeWebSocket.reset();
  const clock = createClock();
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
    crypto: { randomUUID: () => "u-" + Math.random().toString(36).slice(2) },
    URLSearchParams,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    setInterval: () => 0, // disable the 2 Hz publishStatus tick — tests
                          // drive state explicitly via post().
    clearInterval: () => {},
    console: { log: () => {}, warn: () => {}, error: () => {} },
  };
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
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
    const last = chrome._writes[chrome._writes.length - 1];
    return last.bridgeStatus || null;
  }

  // Simulate the operator changing settings in the popup — fires the
  // chrome.storage.onChanged listener the content script registered at
  // boot. Matches the real shape: `{ key: { newValue, oldValue } }`.
  function flipUseTls(useTls) {
    chrome._fireChange({ useTls: { newValue: !!useTls, oldValue: !useTls } });
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
    openSockets: () => FakeWebSocket._all,
    lastSocket: () => FakeWebSocket._all[FakeWebSocket._all.length - 1],
    clock,
    flushMicrotasks,
    flipUseTls,
    indicator,
    detachIndicator,
  };
}

module.exports = { createBridge, FakeWebSocket };
