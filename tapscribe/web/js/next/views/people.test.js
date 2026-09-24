// Unit tests for the People Region model (run via `node --test`, no DOM).
//
// #255: the People region moved from a hand-maintained sig to a Region model
// (templates.js `renderRegionModel`). These pin exactly the dependencies the
// old sig docstring hand-enumerated — the server name, the pending-rename
// overlay, the identities, the sessions SET (not just its length), live /
// recorded, and the focused session. Drop any of them from `peopleModel` and
// the region silently goes stale; that is what these tests now catch at the
// derivation instead of at an e2e audit.

import { test } from "node:test";
import assert from "node:assert/strict";

import { peopleModel } from "./people.js";

/** A /api/state Person row, as thin as the derivation reads it. */
const person = (over = {}) => ({
  id: "p_1",
  name: "Ada",
  named: true,
  identities: ["dev-1"],
  sessions: ["s1"],
  session_count: 1,
  recorded: true,
  live: false,
  ...over,
});

const noPending = () => undefined;

test("the model carries the focused session and every render field per row", () => {
  const model = peopleModel([person()], "s1", noPending);

  assert.equal(model.here, "s1");
  assert.deepEqual(model.people, [
    {
      id: "p_1",
      named: true,
      name: "Ada",
      pending: null,
      identities: ["dev-1"],
      sessions: ["s1"],
      count: 1,
      live: false,
      recorded: true,
    },
  ]);
});

test("a pending rename reaches the serialised model", () => {
  // The old sig's `pendingNames.get(p.id)` term: without it a typed name never
  // re-renders, and the row keeps showing the server's value mid-edit.
  const plain = JSON.stringify(peopleModel([person()], "s1", noPending));
  const edited = JSON.stringify(
    peopleModel([person()], "s1", (id) => (id === "p_1" ? "Ada Lovelace" : undefined)),
  );
  assert.notEqual(edited, plain);
});

test("a sessions-SET change at equal session_count reaches the serialised model", () => {
  // The trap the old comment warned about: `is-here` and the count tooltip read
  // the array, not its length — a length-only term missed ["s1"] → ["s2"].
  const one = JSON.stringify(peopleModel([person({ sessions: ["s1"] })], "s1", noPending));
  const two = JSON.stringify(peopleModel([person({ sessions: ["s2"] })], "s1", noPending));
  assert.notEqual(one, two);
});

test("here flips with the focused session", () => {
  // The `is-here` highlight keys on it; a model that ignored `here` would
  // strand the highlight on the previously focused session.
  const s1 = JSON.stringify(peopleModel([person()], "s1", noPending));
  const s2 = JSON.stringify(peopleModel([person()], "s2", noPending));
  assert.notEqual(s1, s2);
});
