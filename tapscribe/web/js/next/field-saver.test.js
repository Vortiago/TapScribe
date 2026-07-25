// Unit tests for the shared debounced field saver (run via `node --test`).
//
// #355: the "optimistic overlay + debounced PUT + saving/saved/failed badge"
// state machine was copied three times (sessions.js, spine.js, people.js) and
// the copies had already drifted. This pins the ONE machine they now share. It's
// DOM-free by construction — the caller supplies the overlay Map, the PUT, and a
// status target — so the whole lifecycle runs under node:test's mock.timers
// without a browser. The DOM wiring in the views stays covered by the playwright
// sweep; the session-label BINDING is covered by session-labels.test.js, and the
// status lifecycle itself by save-status.test.js.
//
// The frontend tsconfig excludes *.test.js, so this file is never typechecked.

import { describe, it, mock, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import { createFieldSaver, createOverlay, SAVE_DEBOUNCE_MS } from "./field-saver.js";
import { SAVED_BADGE_MS } from "../save-status.js";

// Only setTimeout is mocked, so setImmediate below stays REAL — that's what
// lets a test drain the saver's awaited PUT (a microtask chain) deterministically
// instead of guessing at a number of Promise.resolve() hops.
const drain = () => new Promise((resolve) => setImmediate(resolve));

/** A real overlay over `{id}` records, optionally pre-seeded. The saver needs
 * more than a Map (it claims an id while a save is scheduled or in flight, so the
 * sweep can't retire it mid-save), so tests drive the real thing. */
const idOverlay = (seed = {}) => {
  const overlay = createOverlay({
    idOf: (/** @type {{id: string}} */ r) => r.id,
    baselineFor: () => "",
  });
  for (const [id, value] of Object.entries(seed)) overlay.set(id, value);
  return overlay;
};

/** A status target recording every transition, standing in for the DOM cells.
 * `seen` is the lifecycle as the operator would read it, in order. */
const fakeTarget = () => {
  let text = "";
  /** @type {string[]} */
  const seen = [];
  return {
    set(t) {
      text = t;
      seen.push(t);
    },
    replaceWhen(accepts, to) {
      if (accepts(text)) {
        text = to;
        seen.push(to);
      }
    },
    replace(from, to) {
      if (text === from) {
        text = to;
        seen.push(to);
      }
    },
    get text() {
      return text;
    },
    seen,
  };
};

describe("createFieldSaver", () => {
  beforeEach(() => mock.timers.enable({ apis: ["setTimeout"] }));
  afterEach(() => mock.timers.reset());

  /** A saver with the boring collaborators defaulted, so each case names only
   * what it actually exercises. */
  const makeSaver = (overrides = {}) =>
    createFieldSaver({
      overlay: idOverlay(),
      put: async () => {},
      afterSave: () => {},
      ...overrides,
    });

  it("coalesces a burst of edits into one PUT carrying the latest overlay value", async () => {
    const overlay = idOverlay();
    /** @type {Array<[string, string]>} */
    const puts = [];
    const saver = makeSaver({ overlay, put: async (id, value) => void puts.push([id, value]) });

    // Three keystrokes inside one debounce window, as the input handler does:
    // overlay first (so the field keeps showing what was typed), then save().
    for (const value of ["a", "ab", "abc"]) {
      overlay.set("s1", value);
      saver.save("s1", fakeTarget());
    }
    mock.timers.tick(SAVE_DEBOUNCE_MS - 1);
    assert.deepEqual(puts, [], "must not PUT before the debounce window elapses");

    mock.timers.tick(1);
    await drain();
    assert.deepEqual(puts, [["s1", "abc"]], "one PUT, carrying the newest typed value");
  });

  it("walks the status target saving… → saved → cleared on a successful PUT", async () => {
    const overlay = idOverlay({ s1: "Kickoff" });
    const target = fakeTarget();
    const saver = makeSaver({ overlay });

    saver.save("s1", target);
    mock.timers.tick(SAVE_DEBOUNCE_MS);
    assert.equal(target.text, "saving…", "the badge appears when the PUT starts, not when it's queued");

    await drain();
    assert.equal(target.text, "saved");

    mock.timers.tick(SAVED_BADGE_MS - 1);
    assert.equal(target.text, "saved", "the saved badge lingers long enough to be read");
    mock.timers.tick(1);
    assert.equal(target.text, "", "then clears itself");
    assert.deepEqual(target.seen, ["saving…", "saved", ""]);
  });

  it("reports a rejected PUT as failed: … and never claims saved", async () => {
    const overlay = idOverlay({ s1: "Kickoff" });
    const target = fakeTarget();
    const saver = makeSaver({
      overlay,
      put: async () => {
        throw new Error("500 disk full");
      },
    });

    saver.save("s1", target);
    mock.timers.tick(SAVE_DEBOUNCE_MS);
    await drain();
    // errText strips the "Error: " prefix — the operator reads the server's words.
    assert.equal(target.text, "failed: 500 disk full");

    // A failure must not be swept away by the saved-badge timer either.
    mock.timers.tick(SAVED_BADGE_MS * 3);
    assert.equal(target.text, "failed: 500 disk full", "a failure stays on screen");
    assert.ok(!target.seen.includes("saved"), "a rejected PUT never shows saved");
  });

  it("no-ops silently when the overlay entry is gone by the time the timer fires", async () => {
    const overlay = idOverlay({ s1: "Kickoff" });
    const target = fakeTarget();
    /** @type {string[]} */
    const puts = [];
    const saver = makeSaver({ overlay, put: async (id) => void puts.push(id) });

    saver.save("s1", target);
    // Either the per-tick catch-up sweep dropped the entry (the server caught
    // up), or the record was deleted. Deleting the entry is therefore the ONE
    // way to cancel a pending save — no separate cancel() to keep in step, and
    // it cancels for every saver over this overlay, not just one.
    overlay.forget("s1");
    mock.timers.tick(SAVE_DEBOUNCE_MS);
    await drain();

    assert.deepEqual(puts, [], "a dropped overlay entry is not PUT");
    assert.deepEqual(target.seen, [], "and no status badge flashes for a save that never ran");
  });

  it("debounces per id, so renaming one record never cancels another's save", async () => {
    const overlay = idOverlay();
    /** @type {Array<[string, string]>} */
    const puts = [];
    const saver = makeSaver({ overlay, put: async (id, v) => void puts.push([id, v]) });

    overlay.set("s1", "one");
    saver.save("s1", fakeTarget());
    mock.timers.tick(300);
    overlay.set("s2", "two");
    saver.save("s2", fakeTarget());
    mock.timers.tick(300);
    await drain();
    assert.deepEqual(puts, [["s1", "one"]], "s1's timer runs on its own schedule");

    mock.timers.tick(300);
    await drain();
    assert.deepEqual(puts, [["s1", "one"], ["s2", "two"]], "s2 saves too");
  });

  it("runs afterSave once a save settles, whether it succeeded or failed", async () => {
    const overlay = idOverlay({ ok: "a", bad: "b" });
    let settled = 0;
    const saver = makeSaver({
      overlay,
      put: async (id) => {
        if (id === "bad") throw new Error("boom");
      },
      afterSave: () => void settled++,
    });

    saver.save("ok", fakeTarget());
    mock.timers.tick(SAVE_DEBOUNCE_MS);
    await drain();
    assert.equal(settled, 1, "a successful save reports settlement (the views' afterMutate)");

    saver.save("bad", fakeTarget());
    mock.timers.tick(SAVE_DEBOUNCE_MS);
    await drain();
    assert.equal(settled, 2, "a failed save settles too — the tick must resume either way");
  });
});

// --- createOverlay: the pending-edit half of the contract. The saver reads it;
// the tick sweeps it. Pinned here because BOTH editable resources (session
// labels, People names) now bind it instead of hand-rolling a Map plus a
// per-view sweep loop.

/** A People-shaped record, to prove the overlay is resource-agnostic. */
const person = (id, named, name) => ({ id, named, name });
const namesOverlay = () =>
  createOverlay({ idOf: (p) => p.id, baselineFor: (p) => (p.named ? p.name : "") });

describe("createOverlay", () => {
  it("holds a pending edit until it is forgotten", () => {
    const names = namesOverlay();
    assert.equal(names.get("p1"), undefined, "nothing pending for an untouched record");

    names.set("p1", "Ada");
    assert.equal(names.get("p1"), "Ada");

    names.forget("p1");
    assert.equal(names.get("p1"), undefined);
  });

  it("sweeps away edits the server has caught up to, keyed by the record's own id", () => {
    const names = namesOverlay();
    names.set("p1", "Ada");
    names.set("p2", "Grace");
    names.set("p3", ""); // cleared the name

    names.sweep([
      person("p1", true, "Ada"), // the PUT landed
      person("p2", true, "Gra"), // still being typed
      person("p3", false, "Speaker 2"), // unnamed → baseline "", so caught up
    ]);

    assert.equal(names.get("p1"), undefined);
    assert.equal(names.get("p2"), "Grace", "an unsaved edit survives");
    assert.equal(names.get("p3"), undefined);
  });

  it("will not sweep away an edit while its save is scheduled or in flight", async () => {
    // Edit-then-revert: the first value's PUT is already in flight when the
    // operator reverts, so a tick carrying a snapshot that still shows the
    // ORIGINAL value must NOT retire the revert — that would let the in-flight
    // value win a change the operator explicitly undid.
    mock.timers.enable({ apis: ["setTimeout"] });
    /** @type {Array<[string, string]>} */
    const puts = [];
    const overlay = namesOverlay();
    const saver = createFieldSaver({
      overlay,
      put: async (id, v) => void puts.push([id, v]),
      afterSave: () => {},
    });

    overlay.set("p1", "Ada");
    saver.save("p1", fakeTarget());
    overlay.set("p1", ""); // reverted to the server's baseline for an unnamed Person
    saver.save("p1", fakeTarget());

    overlay.sweep([person("p1", false, "Speaker 1")]); // baseline "" === the pending revert
    assert.equal(overlay.get("p1"), "", "the revert survives a sweep while its save is pending");

    mock.timers.tick(SAVE_DEBOUNCE_MS);
    await drain();
    assert.deepEqual(puts, [["p1", ""]], "and the revert is the value that gets PUT");

    // Once settled the claim is released, so the very next tick retires it.
    overlay.sweep([person("p1", false, "Speaker 1")]);
    assert.equal(overlay.get("p1"), undefined);
    mock.timers.reset();
  });

  it("leaves records absent from the sweep alone", () => {
    const names = namesOverlay();
    names.set("p9", "Typed");
    names.sweep([person("p1", true, "Typed")]);
    assert.equal(names.get("p9"), "Typed");
  });

  it("feeds a saver: forgetting a pending edit cancels its save", async () => {
    mock.timers.enable({ apis: ["setTimeout"] });
    const overlay = namesOverlay();
    /** @type {string[]} */
    const puts = [];
    const saver = createFieldSaver({
      overlay,
      put: async (id) => void puts.push(id),
      afterSave: () => {},
    });

    overlay.set("p1", "Ada");
    saver.save("p1", fakeTarget());
    overlay.forget("p1");
    mock.timers.tick(SAVE_DEBOUNCE_MS);
    await drain();

    assert.deepEqual(puts, [], "the saver re-reads the overlay, so forget() is the cancellation");
    mock.timers.reset();
  });
});
