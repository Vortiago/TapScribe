// Unit tests for the session-label owner (run via `node --test`).
//
// #355: the optimistic rename overlay used to be TWO private Maps — one in
// sessions.js, one in spine.js — both PUTting the same /api/session-meta/{sid}
// {label}, so the two views could disagree about a session's pending rename and
// the release review had to add the same catch-up sweep in both. These pin the
// module that now owns the whole concern: the pending-edit read, the debounced
// PUT, the sweep, and the fact that forgetting an edit is what cancels its save.
// The generic machine underneath has its own tests in field-saver.test.js.
//
// This module holds a SINGLETON overlay and saver (a pending rename belongs to
// the session, not to a view), so each case uses unique session ids. Timers are
// mocked and `fetch`/`document`/`CSS` stubbed, so the real debounced save can be
// driven to completion deterministically instead of leaking a live 600 ms timer
// into a real fetch after the test body returns.
//
// The frontend tsconfig excludes *.test.js, so this file is never typechecked.

import { test, mock, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import { SAVE_DEBOUNCE_MS } from "./field-saver.js";
import {
  dropCaughtUpSessionLabels,
  editSessionLabel,
  forgetSessionLabel,
  pendingOr,
  pendingSessionLabel,
  serverSessionLabel,
  sessionLabelFor,
} from "./session-labels.js";

/** A /api/state session, as thin as these helpers actually read it. */
const sess = (id, label) => ({
  session: id,
  ...(label === undefined ? {} : { session_meta: { label } }),
});

/** PUTs the module made, newest last: [url, parsedBody]. */
let puts = [];
const realFetch = globalThis.fetch;
const realDocument = globalThis.document;
const realCSS = globalThis.CSS;
const drain = () => new Promise((resolve) => setImmediate(resolve));

beforeEach(() => {
  mock.timers.enable({ apis: ["setTimeout"] });
  puts = [];
  globalThis.fetch = async (url, init) => {
    puts.push([String(url), JSON.parse(String(init?.body ?? "null"))]);
    return {
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({}),
    };
  };
  // The saver narrates into the live status cells; there are none here.
  globalThis.document = /** @type {any} */ ({ querySelectorAll: () => [] });
  globalThis.CSS = /** @type {any} */ ({ escape: (/** @type {string} */ s) => s });
});
afterEach(() => {
  mock.timers.reset();
  globalThis.fetch = realFetch;
  globalThis.document = realDocument;
  globalThis.CSS = realCSS;
});

/** Run every scheduled rename save to completion, which also releases the
 * overlay's in-flight claim so a following sweep can retire the entry. */
const settleSaves = async () => {
  mock.timers.tick(SAVE_DEBOUNCE_MS);
  await drain();
};

test("sessionLabelFor shows a pending rename in preference to the server's label", () => {
  const s = sess("s-pref", "Old name");
  assert.equal(sessionLabelFor(s), "Old name"); // nothing pending → the server's label

  editSessionLabel("s-pref", "New name");
  assert.equal(sessionLabelFor(s), "New name"); // a typed rename wins until its PUT lands
  assert.equal(pendingSessionLabel("s-pref"), "New name");
  assert.equal(pendingOr("s-pref", "Old name"), "New name", "and the render-path rule agrees");

  forgetSessionLabel("s-pref");
  assert.equal(pendingSessionLabel("s-pref"), undefined);
  assert.equal(sessionLabelFor(s), "Old name", "forgetting an edit restores the server's label");
  assert.equal(pendingOr("s-pref", "Old name"), "Old name");
});

test("serverSessionLabel reads an unlabelled session as the empty string", () => {
  // No meta at all and meta with an empty label must be indistinguishable —
  // the catch-up sweep compares pending edits against this value.
  assert.equal(serverSessionLabel(sess("s-bare")), "");
  assert.equal(serverSessionLabel(sess("s-blank", "")), "");
  assert.equal(sessionLabelFor(sess("s-bare")), "");
});

test("a rename debounces into one PUT of the newest value", async () => {
  editSessionLabel("s-put", "Kick");
  editSessionLabel("s-put", "Kickoff");
  assert.deepEqual(puts, [], "nothing before the debounce elapses");

  await settleSaves();
  assert.deepEqual(puts, [["/api/session-meta/s-put", { label: "Kickoff" }]]);

  dropCaughtUpSessionLabels([sess("s-put", "Kickoff")]);
  assert.equal(pendingSessionLabel("s-put"), undefined);
});

test("forgetting a rename cancels its save", async () => {
  editSessionLabel("s-gone", "Never sent");
  forgetSessionLabel("s-gone"); // the session was deleted / absorbed
  await settleSaves();
  assert.deepEqual(puts, [], "a queued PUT can't 404 against a folder that's gone");
});

test("dropCaughtUpSessionLabels drops a landed rename and keeps one still in flight", async () => {
  editSessionLabel("s-landed", "Kickoff");
  editSessionLabel("s-inflight", "Retro draft");
  editSessionLabel("s-cleared", "");
  // Their saves must have settled before the sweep can retire anything: an entry
  // whose save is still scheduled is deliberately skipped (field-saver.js's
  // sweep), so a revert can't be dropped by a tick carrying a stale snapshot.
  await settleSaves();

  dropCaughtUpSessionLabels([
    sess("s-landed", "Kickoff"), // the PUT landed; the pending edit is redundant
    sess("s-inflight", "Retro"), // the server hasn't caught up to this one
    sess("s-cleared"), // renamed to empty, and the server has no label
  ]);

  assert.equal(pendingSessionLabel("s-landed"), undefined);
  assert.equal(pendingSessionLabel("s-inflight"), "Retro draft"); // still differs → survives
  assert.equal(pendingSessionLabel("s-cleared"), undefined); // an emptied label is caught up

  forgetSessionLabel("s-inflight");
});

test("dropCaughtUpSessionLabels leaves sessions absent from the tick alone", async () => {
  // A tick that simply doesn't mention a session must not drop its pending edit.
  editSessionLabel("s-elsewhere", "Typed");
  await settleSaves();
  dropCaughtUpSessionLabels([sess("s-other", "Typed")]);
  assert.equal(pendingSessionLabel("s-elsewhere"), "Typed");

  forgetSessionLabel("s-elsewhere");
});
