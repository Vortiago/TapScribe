// The Voice→Person join, pure. `voiceRows` folds the lazy Voices body together
// with the mapping off the poll, and `modeIntent` reads what a click on a Taps
// mode button means. Node's built-in runner, no DOM.

import { test } from "node:test";
import assert from "node:assert/strict";

import { voiceRows } from "./transcript.js";
import { modeIntent } from "./taps.js";

const tap = (identity, name, run_id, voices) => ({ identity, name, run_id, voices });
const voice = (key, label, seconds, spans = 1) => ({ key, label, seconds, spans });
const join = (taps, mapping = {}) => voiceRows(taps, { mapping });

test("an unmapped voice reads as Speaker <label> with its share of the tap", () => {
  const rows = join([tap("sysaudio", "Them", "r1", [voice("sysaudio#A", "A", 30), voice("sysaudio#B", "B", 10)])]);

  assert.deepEqual(
    rows.map((r) => [r.key, r.label, r.pct, r.personId]),
    [["sysaudio#A", "Speaker A", 75, ""], ["sysaudio#B", "Speaker B", 25, ""]],
  );
});

test("a mapped voice carries its Person id", () => {
  const rows = join([tap("sysaudio", "Them", "r1", [voice("sysaudio#A", "A", 30)])], {
    "sysaudio#A": { person_id: "p1", run_id: "r1" },
  });

  assert.equal(rows[0].personId, "p1");
  assert.equal(rows[0].stale, false);
});

test("a mapping from an earlier run is stale — the server stops applying it", () => {
  // The dangerous silence: without this the operator sees `Speaker A` come back
  // with no explanation and no reason to re-map.
  const rows = join([tap("sysaudio", "Them", "r2", [voice("sysaudio#A", "A", 30)])], {
    "sysaudio#A": { person_id: "p1", run_id: "r1" },
  });

  assert.equal(rows[0].stale, true);
});

test("a tap naming no run supersedes nothing, so its mappings still apply", () => {
  // The server's rule (`name_resolution._mapping_applies`): a sidecar that names
  // no run for the identity leaves its mappings applied. Spelling it differently
  // here would tell the operator to re-map one the transcript is honouring.
  const rows = join([tap("sysaudio", "Them", "", [voice("sysaudio#A", "A", 30)])], {
    "sysaudio#A": { person_id: "p1", run_id: "r1" },
  });

  assert.equal(rows[0].stale, false);
  assert.equal(rows[0].personId, "p1");
});

test("the tap name rides the label only when a session has more than one", () => {
  const one = join([tap("sysaudio", "Them", "r1", [voice("sysaudio#A", "A", 1)])]);
  const two = join([
    tap("sysaudio", "Them", "r1", [voice("sysaudio#A", "A", 1)]),
    tap("room", "Room mic", "r1", [voice("room#A", "A", 1)]),
  ]);

  assert.equal(one[0].label, "Speaker A");
  assert.deepEqual(two.map((r) => r.label), ["Them · Speaker A", "Room mic · Speaker A"]);
});

test("a silent voice does not divide by zero", () => {
  const rows = join([tap("sysaudio", "Them", "r1", [voice("sysaudio#A", "A", 0)])]);

  assert.equal(rows[0].pct, 0);
});

test("no taps is no rows", () => {
  assert.deepEqual(join([]), []);
});

// ---- modeIntent ------------------------------------------------------------

/** A minimal stand-in for the two DOM reads `modeIntent` makes. */
const btn = (mode, pressed, identity = "sysaudio") => ({
  dataset: { mode },
  getAttribute: (a) => (a === "aria-pressed" ? (pressed ? "true" : "false") : null),
  closest: () => (identity ? { dataset: { identity } } : null),
});

test("clicking the mode a tap is not in is the intent to change it", () => {
  assert.deepEqual(modeIntent(btn("multi", false)), { identity: "sysaudio", mode: "multi" });
});

test("clicking the mode a tap is already in means nothing", () => {
  // Otherwise every repaint's click would PUT the value it already has.
  assert.equal(modeIntent(btn("multi", true)), null);
});

test("a button outside a row, or naming no mode, means nothing", () => {
  assert.equal(modeIntent(btn("multi", false, "")), null);
  assert.equal(modeIntent(btn("sideways", false)), null);
  assert.equal(modeIntent(null), null);
});
