// Unit tests for the active-taps toggle-click helper (run via `node --test`).
//
// toggleIntent is the click-validation + next-value branching factored out of
// wireToggles so it's exercisable without a DOM — a plain {disabled, dataset}
// object stands in for the button, the same shortcut shell.test.js uses for
// its WeakMap-keyed host. wireToggles itself (DOM delegation + the PUT) is
// left to the playwright dashboard e2e, which drives the real button markup
// against both hosts it's bound to (the rail + the Taps view).

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { toggleIntent } from "./active-taps.js";

/** Build a `.tap-toggle` button stand-in. */
const btn = (over) => ({ disabled: false, dataset: {}, ...over });

describe("toggleIntent", () => {
  it("returns null when the button is disabled", () => {
    assert.equal(
      toggleIntent(btn({ disabled: true, dataset: { identity: "alice", toggle: "record" } })),
      null,
    );
  });

  it("returns null when identity is missing", () => {
    assert.equal(toggleIntent(btn({ dataset: { toggle: "record" } })), null);
  });

  it("returns null when the toggle kind is missing", () => {
    assert.equal(toggleIntent(btn({ dataset: { identity: "alice" } })), null);
  });

  it("flips off -> on when state is unset (defaults to off)", () => {
    assert.deepEqual(toggleIntent(btn({ dataset: { identity: "alice", toggle: "record" } })), {
      identity: "alice",
      which: "record",
      next: true,
    });
  });

  it("flips off -> on when state is '0'", () => {
    assert.deepEqual(
      toggleIntent(btn({ dataset: { identity: "bob", toggle: "record", state: "0" } })),
      { identity: "bob", which: "record", next: true },
    );
  });

  it("flips on -> off when state is '1'", () => {
    assert.deepEqual(
      toggleIntent(btn({ dataset: { identity: "bob", toggle: "live", state: "1" } })),
      { identity: "bob", which: "live", next: false },
    );
  });
});
