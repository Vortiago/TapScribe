// Unit tests for the People Region model (#255, no DOM). Every field a row
// renders from must vary the serialised model, or the region goes stale, and
// `pregRowView` must read the model as the row shows it.

import { test } from "node:test";
import assert from "node:assert/strict";

import { peopleModel, pregRowView } from "./people.js";

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
      live: false,
      recorded: true,
    },
  ]);
});

/** The serialised model, which is the region's swap gate, for one Person. */
const sig = (over = {}, here = "s1", pendingOf = noPending) =>
  JSON.stringify(peopleModel([person(over)], here, pendingOf));

test("every field a row renders from varies the serialised model", () => {
  const base = sig();
  for (const [what, changed] of [
    ["name", sig({ name: "Grace" })],
    ["named", sig({ named: false })],
    ["identities", sig({ identities: ["dev-2"] })],
    ["live", sig({ live: true })],
    ["recorded", sig({ recorded: false })],
    // The old sig's `pendingNames.get(p.id)` term: without it a typed name
    // never re-renders, and the row keeps showing the server's value mid-edit.
    ["a pending rename", sig({}, "s1", (id) => (id === "p_1" ? "Ada Lovelace" : undefined))],
    // `is-here` and the count tooltip read the array, not its length: a
    // length-only term missed ["s1"] → ["s2"] at equal session_count.
    ["a sessions-SET change", sig({ sessions: ["s2"] })],
    // The `is-here` highlight keys on it: a model that ignored `here` would
    // strand the highlight on the previously focused session.
    ["the focused session", sig({}, "s2")],
    // Clearing the field is an edit: it must not collide with no edit at all.
    ["a pending empty name", sig({}, "s1", () => "")],
  ]) {
    assert.notEqual(changed, base, `${what} must reach the sig`);
  }
});

/** One model row, from a Person with `over` applied. */
const row = (over = {}, pending = undefined) => peopleModel([person(over)], "", () => pending).people[0];

test("the input shows a pending edit, else the chosen name, else nothing", () => {
  assert.equal(pregRowView(row({}, "Ada L"), "").shown, "Ada L");
  assert.equal(pregRowView(row({}, ""), "").shown, "", "a cleared field stays cleared");
  assert.equal(pregRowView(row(), "").shown, "Ada");
  assert.equal(pregRowView(row({ named: false, name: "Mic 1" }), "").shown, "");
});

test("the placeholder falls back from the name to the first identity", () => {
  assert.equal(pregRowView(row({ named: false, name: "Mic 1" }), "").placeholder, "Mic 1");
  assert.equal(pregRowView(row({ name: "", identities: ["dev-9"] }), "").placeholder, "dev-9");
  assert.equal(pregRowView(row({ name: "", identities: [] }), "").placeholder, "name…");
});

test("the avatar fallback is the default label, and empty when there is none", () => {
  assert.equal(pregRowView(row({ named: false, name: "Mic 1" }), "").fallback, "Mic 1");
  assert.equal(pregRowView(row({ name: "", identities: ["dev-9"] }), "").fallback, "dev-9");
  assert.equal(pregRowView(row({ name: "", identities: [] }), "").fallback, "");
});

test("the source label reads live, else recorded, else a dash", () => {
  /** @param {{ src: string, srcClass: string }} v */
  const srcOf = (v) => [v.src, v.srcClass];
  assert.deepEqual(srcOf(pregRowView(row({ live: true }), "")), ["● live", "is-live"]);
  assert.deepEqual(srcOf(pregRowView(row(), "")), ["recorded", "is-recorded"]);
  assert.deepEqual(srcOf(pregRowView(row({ recorded: false }), "")), ["—", "is-recorded"]);
});

test("is-here marks a member of the focused session, and nothing when none is focused", () => {
  assert.equal(pregRowView(row({ sessions: ["s1"] }), "s1").isHere, true);
  assert.equal(pregRowView(row({ sessions: ["s1"] }), "s2").isHere, false);
  assert.equal(pregRowView(row({ sessions: [""] }), "").isHere, false);
});

test("the count reads the sessions list", () => {
  assert.equal(pregRowView(row({ sessions: ["s1"] }), "").count, "1 session");
  assert.equal(pregRowView(row({ sessions: ["s1", "s2"] }), "").count, "2 sessions");
});
