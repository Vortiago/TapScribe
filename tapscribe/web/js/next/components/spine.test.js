// Unit tests for the spine's pure derivations (run via `node --test`, no DOM).
//
// peopleCount, realMilestones and journeyProgress are pure helpers factored out
// of globalDefs/journeyDefs/render so the People-count, Summary-milestone and
// progress-bar logic is exercised without a browser — same pattern as
// shell.test.js's headerNeedsRender / nextRecordingEnabled.

import { test } from "node:test";
import assert from "node:assert/strict";

import { journeyProgress, peopleCount, realMilestones } from "./spine.js";

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

// ---- journeyProgress --------------------------------------------------------
// #411 made the per-stage ✓ follow the table's `milestone` key, leaving the bar
// and caption hand-counting four flags: a fifth milestone would light a ✓ while
// the caption still read n/4. Both counts now derive from the Milestones object,
// and these rungs pin the DERIVATION rather than the literal 4, so the next
// milestone cannot reintroduce the split.

test("journeyProgress: the denominator IS the milestone key count, not a literal", () => {
  const total = Object.keys(realMilestones(null)).length;
  for (const sess of [null, {}, { wav_count: 3, stripped: true }]) {
    assert.equal(journeyProgress(sess).total, total, "total drifted from realMilestones' keyset");
  }
});

test("journeyProgress: the numerator counts exactly the reached milestones", () => {
  assert.deepEqual(journeyProgress(null), { reached: 0, total: 4, pct: 0 });
  assert.deepEqual(journeyProgress({ wav_count: 1 }), { reached: 1, total: 4, pct: 25 });
  assert.deepEqual(journeyProgress({ wav_count: 1, stripped: true }), { reached: 2, total: 4, pct: 50 });
  assert.deepEqual(
    journeyProgress({
      wav_count: 1,
      stripped: true,
      session_transcript: {},
      session_summary: { summarized_at: "2026-07-01T00:00:00+00:00" },
    }),
    { reached: 4, total: 4, pct: 100 },
  );
});

test("journeyProgress: a half-done milestone set never reads 100%", () => {
  // The bar's whole job is "% of real work", so a session with a transcript but
  // no summary must not paint full — the pre-#326 bug class (bar hardcoded to a
  // stage count below the real one) reads 100% early.
  const { pct } = journeyProgress({ wav_count: 1, stripped: true, session_transcript: {} });
  assert.ok(pct > 0 && pct < 100, `three of four milestones must read partial, got ${pct}%`);
});
