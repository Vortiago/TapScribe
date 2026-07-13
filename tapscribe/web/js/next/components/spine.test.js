// Unit tests for the spine's pure derivations (run via `node --test`, no DOM).
//
// peopleCount and realMilestones are pure helpers factored out of
// globalDefs/journeyDefs so the People-count and Summary-milestone logic is
// exercised without a browser — same pattern as shell.test.js's
// headerNeedsRender / nextRecordingEnabled.

import { test } from "node:test";
import assert from "node:assert/strict";

import { peopleCount, realMilestones } from "./spine.js";

// ---- peopleCount ------------------------------------------------------------
// #226: the People chip must derive from the ADR-0009 registry (`j.people`),
// never from the pre-ADR-0009 `session_meta.aliases` shadow join.

test("peopleCount is 0 for an AppState with no people yet", () => {
  assert.equal(peopleCount({}), 0);
  assert.equal(peopleCount({ people: [] }), 0);
});

test("peopleCount returns the registry's length", () => {
  const j = { people: [{ id: "p_1" }, { id: "p_2" }, { id: "p_3" }] };
  assert.equal(peopleCount(j), 3);
});

test("peopleCount ignores session_meta.aliases entirely — the pre-ADR-0009 shadow join must not resurrect a count", () => {
  const j = {
    people: [],
    sessions: [
      { session: "s1", session_meta: { aliases: { Alice_Smith: "Alice", Bob_Jones: "Bob" } } },
      { session: "s2", session_meta: { aliases: { Carol_Lee: "Carol" } } },
    ],
  };
  // Three distinct alias keys exist across sessions, but the registry (the
  // ADR-0009 source of truth) is empty — the count must follow the registry.
  assert.equal(peopleCount(j), 0);
});

// ---- realMilestones ---------------------------------------------------------
// #226: Summary is wired (#83/#84/#85/#86) and ships a `session_summary`
// marker on /api/state — the milestone must reflect it, not stay hardcoded.

test("realMilestones: summarized is false for a session with no session_summary marker", () => {
  const ms = realMilestones({ wav_count: 1, stripped: true, session_transcript: {}, session_summary: null });
  assert.equal(ms.summarized, false);
});

test("realMilestones: summarized is true once the session_summary marker carries a summarized_at stamp", () => {
  const ms = realMilestones({
    wav_count: 1,
    stripped: true,
    session_transcript: {},
    session_summary: { summarized_at: "2026-07-01T00:00:00+00:00", source: "local", model: "" },
  });
  assert.equal(ms.summarized, true);
});

test("realMilestones: summarized stays false when session_summary exists but summarized_at is null (malformed on-disk JSON)", () => {
  const ms = realMilestones({
    wav_count: 1,
    session_summary: { summarized_at: null, source: "local", model: "" },
  });
  assert.equal(ms.summarized, false);
});

test("realMilestones: null session (no session focused) never throws and reports all-false", () => {
  const ms = realMilestones(null);
  assert.deepEqual(ms, { captured: false, stripped: false, transcribed: false, summarized: false });
});
