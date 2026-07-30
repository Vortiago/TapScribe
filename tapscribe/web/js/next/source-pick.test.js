// #354 — the original/stripped source pick is ONE session-scoped store, not a
// private Map per view.
//
// `effectiveSource` was already lifted to next/shell.js as a pure function, but
// the state it reads stayed forked: recordings.js and transcript.js each build
// their own `new Map()`. The computation is shared while the source of truth is
// not, so the two views can disagree about the SAME session.
//
// THE DECISION THIS CONTRACT MAKES: the pick belongs to the SESSION, not to the
// view. The issue left it open (shared, or deliberately per-view + documented);
// shared is the reading it calls "arguably the correct mental model" — it is
// *the session's* source. One store in shell.js next to effectiveSource, both
// views read and write it through these accessors.
//
// The cross-VIEW half of the contract is an e2e — two view closures agreeing is
// not observable from here. See tests/e2e/test_dashboard_source_pick.py.
//
// Node's built-in runner, no DOM: shell.js only touches document/tpl inside
// functions, so importing it is side-effect-free (same as shell.test.js). This
// file is excluded from the frontend tsconfig, so the minimal session
// stand-ins below never hit tsc.

import { test } from "node:test";
import assert from "node:assert/strict";

import { clearSourcePick, effectiveSource, setSourcePick } from "./shell.js";

/** A minimal Session as effectiveSource reads it: an id, and whether the
 * session has a stripped/ folder. */
const sess = (id, stripped = { count: 3, stripped_at: "2026-01-01T00:00:00Z" }) => ({
  session: id,
  stripped,
});

test("a session nobody has picked for reads as the original", () => {
  assert.equal(effectiveSource(sess("s-untouched")), "original");
});

test("a null session reads as the original", () => {
  // The spine can be between sessions; the old two-arg form guarded this with
  // `session?.session || ""` and the lift must not drop it.
  assert.equal(effectiveSource(null), "original");
});

test("setSourcePick makes that session read as stripped", () => {
  setSourcePick("s-picked", "stripped");
  assert.equal(effectiveSource(sess("s-picked")), "stripped");
});

test("the pick is scoped to ONE session, not global", () => {
  // Distinguishing: a single module-level scalar — the simplest way to "share"
  // a pick — passes every test above and fails this one.
  setSourcePick("s-a", "stripped");
  assert.equal(effectiveSource(sess("s-a")), "stripped");
  assert.equal(effectiveSource(sess("s-b")), "original", "picking for one session must not move another");
});

test("a stripped pick on a session with no stripped/ folder still reads original", () => {
  // The pre-existing fallback: a stale pick must not operate on nothing after
  // the clips were cleared. It has to survive being lifted into the store.
  setSourcePick("s-nostrip", "stripped");
  assert.equal(effectiveSource(sess("s-nostrip", null)), "original");
});

test("clearSourcePick returns a session to the original", () => {
  setSourcePick("s-cleared", "stripped");
  assert.equal(effectiveSource(sess("s-cleared")), "stripped");
  clearSourcePick("s-cleared");
  assert.equal(effectiveSource(sess("s-cleared")), "original");
});

test("picking original explicitly reads as original", () => {
  setSourcePick("s-back", "stripped");
  setSourcePick("s-back", "original");
  assert.equal(effectiveSource(sess("s-back")), "original");
});

test("effectiveSource takes the session alone — no caller-supplied map", () => {
  // The anti-fork pin. A second parameter is what let each view hand in its
  // own Map; keeping it (and passing one shared map around) would satisfy
  // every behavioural test above while leaving the fork one call site away.
  assert.equal(effectiveSource.length, 1);
});
