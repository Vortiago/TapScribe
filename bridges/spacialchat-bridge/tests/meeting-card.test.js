// Tests for the popup meeting card (module ④) — the payoff that shows live
// pipeline progress and the finished summary without ever touching the
// dashboard. popup.js is a flat script (no exports), so — like popup.test.js
// — we load it into a vm with a mocked browser environment and drive it
// through its public seams: the chrome.storage it reads, the fetch the
// control-client issues, and the DOM it renders.
//
// The card holds NO local summary cache: it re-derives everything from the
// stored Session id on each open by polling the recorder, mapping the raw
// poll through the pure view-model mapper (pipeline-view.js). These tests
// drive the poll responses and assert the rendered DOM.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const CONTROL_CLIENT_JS = path.join(__dirname, "..", "control-client.js");
const PIPELINE_VIEW_JS = path.join(__dirname, "..", "pipeline-view.js");
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
    hidden: false,
    disabled: false,
    addEventListener: (ev, fn) => {
      (listeners[ev] || (listeners[ev] = [])).push(fn);
    },
    _fire: (ev, arg) => {
      for (const fn of listeners[ev] || []) fn(arg || {});
    },
  };
}

const DETACHED_OK_BODY = {
  ok: true, detached: true, session: "2026-06-19T10-00-00Z", path: "recordings/2026-06-19T10-00-00Z",
};

// Build a popup with a programmable pipeline-poll body and controllable
// timers (so a test can fire the next scheduled poll deterministically).
function createCard({ settings = {}, poll } = {}) {
  const store = {
    recorderHost: "localhost",
    recorderPort: 9999,
    tapToken: "tok",
    useTls: false,
    ...settings,
  };
  let pollBody = poll || { ok: true, state: "idle" };

  const els = {};
  const fetchCalls = [];
  const storageWrites = [];
  const changeListeners = [];
  const clipboardWrites = [];

  // Minimal timer registry: setTimeout records the callback; tick() fires
  // every pending callback once. After a settled poll the control-client has
  // already cleared its abort timer, so the only callback left is the next
  // scheduled poll — firing it re-polls with the current pollBody.
  let timerId = 1;
  const timers = new Map();

  class FakeWS {
    constructor(url, protocols) {
      this.url = url;
      this.protocols = protocols;
      this.onopen = null;
      this.onerror = null;
      this.onclose = null;
      Promise.resolve().then(() => { if (this.onopen) this.onopen({}); });
    }
    close() {}
  }

  const sandbox = {
    document: { getElementById: (id) => els[id] || (els[id] = makeEl(id)) },
    navigator: {
      clipboard: { writeText: (t) => { clipboardWrites.push(t); return Promise.resolve(); } },
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
      tabs: { create: () => {} },
    },
    fetch: (url, opts) => {
      const method = (opts && opts.method) || "GET";
      fetchCalls.push({ url, method, options: opts || {} });
      if (/\/api\/tap\/new-session$/.test(url) && method === "POST") {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(DETACHED_OK_BODY) });
      }
      if (/\/api\/tap\/sessions\/[^/]+\/pipeline$/.test(url)) {
        const body = pollBody;
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ status: "ok" }) });
    },
    WebSocket: FakeWS,
    AbortController: class {
      constructor() { this.signal = { addEventListener() {} }; }
      abort() {}
    },
    URLSearchParams,
    setTimeout: (fn) => { const id = timerId++; timers.set(id, fn); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    setInterval: () => 0,
    clearInterval: () => {},
    window: { addEventListener: () => {} },
    console: { log() {}, warn() {}, error() {} },
  };
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(CONTROL_CLIENT_JS, "utf8"), sandbox, { filename: "control-client.js" });
  vm.runInContext(fs.readFileSync(PIPELINE_VIEW_JS, "utf8"), sandbox, { filename: "pipeline-view.js" });
  vm.runInContext(fs.readFileSync(POPUP_JS, "utf8"), sandbox, { filename: "popup.js" });

  async function settle() {
    for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r));
  }

  return {
    els,
    fetchCalls,
    storageWrites,
    clipboardWrites,
    settle,
    el: (id) => els[id] || (els[id] = makeEl(id)),
    click: (id) => (els[id] || makeEl(id))._fire("click", { preventDefault() {} }),
    setPoll: (body) => { pollBody = body; },
    pollCalls: () => fetchCalls.filter((c) => /\/pipeline$/.test(c.url)),
    // Fire every pending timer (the next scheduled poll) — simulates the
    // interval tick the card uses while the pipeline is running.
    tick: () => {
      const fns = [...timers.values()];
      timers.clear();
      for (const fn of fns) { try { fn(); } catch (e) { /* surface via assert */ } }
    },
    fireChange: (changes) => {
      for (const fn of changeListeners) fn(changes, "local");
    },
  };
}

const RUNNING_TRANSCRIBE = (current, total) => ({
  ok: true, state: "running", stage: "transcribe", status: "transcribing", current, total,
});
const DONE = (text) => ({
  ok: true, state: "done", summary: { summary: text, source: "local", model: "qwen3-0.6b" },
});

// ---- polls on open + renders progress -------------------------------------

test("on open with a stored Session id the card polls and renders stage progress", async () => {
  const c = createCard({
    settings: { meetingSessionId: "sess-1", meetingActive: false },
    poll: RUNNING_TRANSCRIBE(3, 12),
  });
  await c.settle();

  const polls = c.pollCalls();
  assert.ok(polls.length >= 1, "polled the pipeline on open");
  assert.equal(polls[0].url, "http://localhost:9999/api/tap/sessions/sess-1/pipeline");
  assert.equal(polls[0].options.headers.Authorization, "Bearer tok", "poll carries the bearer token");

  assert.equal(c.el("meetingCard").hidden, false, "card is shown while running");
  assert.match(c.el("meetingProgress").textContent, /Transcribing 3\/12/);
});

test("the card polls on an interval while running, updating progress in place", async () => {
  const c = createCard({
    settings: { meetingSessionId: "sess-1", meetingActive: false },
    poll: RUNNING_TRANSCRIBE(1, 4),
  });
  await c.settle();
  const progressNode = c.el("meetingProgress");
  assert.match(progressNode.textContent, /Transcribing 1\/4/);

  // The recorder advances; the next scheduled poll picks it up.
  c.setPoll(RUNNING_TRANSCRIBE(2, 4));
  c.tick();
  await c.settle();

  assert.equal(c.el("meetingProgress"), progressNode, "same node — progress updated in place, not rebuilt");
  assert.match(progressNode.textContent, /Transcribing 2\/4/);
});

// ---- finished summary + reopen survival -----------------------------------

test("reopening the popup from the stored id shows the finished summary (no local cache)", async () => {
  // Models a closed-then-reopened popup: a fresh popup with only the durable
  // meetingSessionId in storage re-derives the done summary from the poll.
  const c = createCard({
    settings: { meetingSessionId: "sess-1", meetingActive: false },
    poll: DONE("We agreed to ship on Friday."),
  });
  await c.settle();

  assert.equal(c.el("meetingCard").hidden, false);
  assert.equal(c.el("meetingSummaryPane").hidden, false, "summary pane shown on done");
  assert.equal(c.el("meetingSummaryText").textContent, "We agreed to ship on Friday.");
  assert.match(c.el("meetingSummaryMeta").textContent, /qwen3-0\.6b/, "light model metadata shown");
});

test("a poll tick does not clobber a mid-copy text selection in the summary pane", async () => {
  const c = createCard({
    settings: { meetingSessionId: "sess-1", meetingActive: false },
    poll: DONE("Original summary."),
  });
  await c.settle();
  const textNode = c.el("meetingSummaryText");
  assert.equal(textNode.textContent, "Original summary.");

  // The user is mid-copy: the rendered text is what they're selecting. A
  // later poll tick (still done) must NOT rewrite the pane — the render-once
  // guard keeps the node untouched so the selection survives.
  textNode.textContent = "Original summary. [USER SELECTING]";
  c.fireChange({ meetingEnd: { newValue: { phase: "started", sessionId: "sess-1" } } });
  await c.settle();

  assert.equal(c.el("meetingSummaryText"), textNode, "same summary node");
  assert.equal(
    textNode.textContent,
    "Original summary. [USER SELECTING]",
    "the summary pane was not rebuilt under the selection",
  );
});

test("Copy copies the finished summary text to the clipboard", async () => {
  const c = createCard({
    settings: { meetingSessionId: "sess-1", meetingActive: false },
    poll: DONE("Action items: 1) ship 2) celebrate."),
  });
  await c.settle();

  c.click("meetingCopy");
  await c.settle();

  assert.equal(c.clipboardWrites.length, 1, "wrote once to the clipboard");
  assert.equal(c.clipboardWrites[0], "Action items: 1) ship 2) celebrate.");
});

// ---- failure --------------------------------------------------------------

test("a failed stage surfaces its name and a human-readable reason", async () => {
  const c = createCard({
    settings: { meetingSessionId: "sess-1", meetingActive: false },
    poll: { ok: true, state: "failed", stage: "transcribe", error: "boom", error_kind: "NoUsableWavs" },
  });
  await c.settle();

  assert.equal(c.el("meetingCard").hidden, false);
  const fail = c.el("meetingFailure");
  assert.equal(fail.hidden, false);
  assert.match(fail.textContent, /transcribe/, "names the failing stage");
  assert.match(fail.textContent, /no usable audio/i, "human-readable reason");
});

// ---- dismiss --------------------------------------------------------------

test("Dismiss clears the durable meeting state and hides the card", async () => {
  const c = createCard({
    settings: { meetingSessionId: "sess-1", meetingActive: false },
    poll: DONE("Done."),
  });
  await c.settle();
  assert.equal(c.el("meetingCard").hidden, false, "card shown before dismiss");

  c.click("meetingDismiss");
  await c.settle();

  const cleared = c.storageWrites.find((w) => "meetingSessionId" in w && w.meetingSessionId === null);
  assert.ok(cleared, "Dismiss cleared the stored meetingSessionId");
  assert.equal(cleared.meetingEnd, null, "and cleared the stored end outcome");
  assert.equal(c.el("meetingCard").hidden, true, "card hidden after dismiss");
});

test("while still recording the card stays hidden and Dismiss is not offered", async () => {
  // A meeting is active but no pipeline has run yet: the poll is idle, which
  // the mapper resolves to 'recording' given the active hint. The card has
  // nothing to show — the status line covers 'Meeting active'.
  const c = createCard({
    settings: { meetingSessionId: "sess-live", meetingActive: true },
    poll: { ok: true, state: "idle" },
  });
  await c.settle();

  assert.equal(c.el("meetingCard").hidden, true, "no card while merely recording");
});

// ---- stale state cleared on a new Start -----------------------------------

test("a new Start clears the previous meeting's card", async () => {
  const c = createCard({
    settings: { meetingSessionId: "sess-old", meetingActive: false },
    poll: DONE("Old meeting summary."),
  });
  await c.settle();
  assert.equal(c.el("meetingSummaryPane").hidden, false, "old summary shown");

  // Operator clicks Start meeting → mints a new detached Session.
  c.setPoll({ ok: true, state: "idle" }); // new session has no pipeline yet
  c.click("startMeeting");
  await c.settle();

  assert.equal(c.el("meetingSummaryPane").hidden, true, "previous summary cleared on Start");
  assert.equal(c.el("meetingCard").hidden, true, "card reset for the fresh meeting");
});
