// RED contract for #411: the per-stage checkmark must come from the view table,
// not from four hand-typed `id === "..."` branches.
//
// #252 folded the Stages view list into shell.js's VIEWS, but spine.js's
// `journeyDefs` still decides `done` by branching on the id:
//
//   if (id === "capture") base.done = captured;
//   if (id === "recordings") base.done = stripped;
//   if (id === "transcript") base.done = transcribed;
//   if (id === "summary") base.done = summarized;
//
// so a ninth journey view needs an edit HERE as well as a VIEWS entry — the
// drift #252's closure implied was gone. The fix carries the milestone key on
// the table entry and reads `realMilestones(sess)[entry.milestone]`.
//
// HOW THIS GOES RED WITHOUT ADDING A NINTH VIEW. `VIEWS` is an exported Map and
// `JOURNEY_VIEWS` an exported array, so a test can INJECT a ninth journey view
// at runtime — the exact scenario the issue names — and assert its checkmark
// tracks the milestone its own table entry declares. At base the injected view
// gets no `done` at all, because no branch names it. (`JOURNEY_VIEWS` is
// computed once at module load, so an injection must reach both.)
//
// WHY THE INJECTED ENTRY IS NOT GAMEABLE. Its id appears nowhere in the source,
// so no id-keyed branch or id-keyed lookup table can serve it. And no view's id
// equals its milestone key (capture→captured, recordings→stripped,
// transcript→transcribed, summary→summarized), so `realMilestones(sess)[id]`
// fails too. The only implementation that passes reads the key off the entry.
//
// THE SEAM. `journeyDefs` is module-private today and this contract calls it by
// name, so the fix must export it. That follows the file's own precedent —
// `peopleCount` and `realMilestones` are both exported as pure helpers "so it's
// unit-testable without a DOM" — and it is the ONLY new API this contract
// requires. What the table field is called, and how the entry types it, stay the
// plan's to choose.
//
// RUNG STATUS, stated honestly: only the LAST test is a guardrail (green before
// AND after). Every test that calls `journeyDefs` is red at base for the export
// reason before the mapping reason, which is why the export has its own rung
// first — the gate should read as one clear gradient, not four copies of the
// same failure.
//
// Gate: the profile's `gates.dashboard_js` — node --test + check-conventions +
// check-slots + check-css-vars + `cd frontend && npm run typecheck`. tsc is in
// that gate and this repo runs `strict` + `noUncheckedIndexedAccess` with
// `checkJs`, so the entry's milestone field has to be typed such that indexing
// `realMilestones()`'s result with it type-checks. (tsconfig excludes
// `**/*.test.js`, so this file itself is not type-checked — the injected entry
// below deliberately carries a field and an id the production types do not know.)

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { VIEWS, JOURNEY_VIEWS, ALL_VIEWS, GLOBAL_VIEWS } from "../shell.js";
import * as spine from "./spine.js";

const src = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

/** The four milestones read four DIFFERENT session fields (see realMilestones),
 * so each is independently settable — which is what lets a test turn exactly one
 * on and watch exactly one checkmark. */
function sessionWith(...on) {
  return {
    wav_count: on.includes("captured") ? 1 : 0,
    stripped: on.includes("stripped"),
    session_transcript: on.includes("transcribed") ? {} : null,
    session_summary: on.includes("summarized") ? { summarized_at: "2026-01-01T00:00:00Z" } : null,
  };
}

/** A quiet /api/state: no live taps, no sessions, no registry. The chips read
 * these, `done` does not. */
const STATE = { active: [], sessions: [], people: [] };

/** Run `fn` with a ninth journey view present in the table, then remove it.
 * Node runs one file's tests sequentially in a SINGLE process, so a leaked
 * injection would be visible to every later test here — hence the finally. */
function withNinthView(entry, fn) {
  VIEWS.set("ninth", {
    group: "journey",
    name: "Ninth",
    lead: "9",
    template: "/web/components/next/ninth.html",
    ...entry,
  });
  JOURNEY_VIEWS.push("ninth");
  try {
    return fn();
  } finally {
    VIEWS.delete("ninth");
    JOURNEY_VIEWS.splice(JOURNEY_VIEWS.indexOf("ninth"), 1);
  }
}

const defFor = (defs, id) => defs.find((d) => d.id === id);

const exported = () =>
  assert.equal(
    typeof spine.journeyDefs,
    "function",
    "spine.js does not export journeyDefs, so the per-stage checkmark cannot be " +
      "driven from a test — export it as a pure helper, like peopleCount and realMilestones",
  );

test("journeyDefs is exported and answers for every journey view", () => {
  exported();
  const defs = spine.journeyDefs(STATE, sessionWith());
  assert.deepEqual(
    defs.map((d) => d.id),
    [...JOURNEY_VIEWS],
    "journeyDefs must produce one entry per journey view, in table order",
  );
});

test("a ninth journey view's checkmark follows the milestone its table entry declares", () => {
  exported();
  withNinthView({ milestone: "stripped" }, () => {
    // TRUE case: the declared milestone is reached, so the checkmark lights.
    const on = defFor(spine.journeyDefs(STATE, sessionWith("stripped")), "ninth");
    assert.ok(on, "the injected journey view produced no nav entry at all");
    assert.equal(
      on.done,
      true,
      "the ninth view declares milestone 'stripped' and the session IS stripped, but its " +
        "checkmark did not light — `done` is still decided by a hand-typed id branch, which is " +
        "exactly the edit #411 says a ninth view should not need",
    );
    // FALSE case with a DIFFERENT milestone reached, so that neither "any
    // progress at all" nor a wrong-key read can pass: stripped is false while
    // transcribed is true. `done` must be a boolean here, not left undefined —
    // the four existing views always carry one.
    const off = defFor(spine.journeyDefs(STATE, sessionWith("transcribed")), "ninth");
    assert.equal(
      off.done,
      false,
      "the ninth view's checkmark must follow its OWN declared milestone ('stripped') — not " +
        "another view's ('transcribed'), and not 'some milestone was reached'",
    );
  });
});

test("each of the four existing journey views keeps its own milestone", () => {
  // The old hand-typed mapping, named verbatim so that removed-minus-derived is
  // provably empty. A derived set is a hypothesis: #252 derived a list from this
  // same table and silently dropped a member, and a table typo here (transcript
  // → summarized) yields `undefined` — a checkmark that never lights, which no
  // other rung would catch.
  const OLD_MAPPING = [
    ["capture", "captured"],
    ["recordings", "stripped"],
    ["transcript", "transcribed"],
    ["summary", "summarized"],
  ];
  exported();
  for (const [id, milestone] of OLD_MAPPING) {
    const defs = spine.journeyDefs(STATE, sessionWith(milestone));
    assert.equal(defFor(defs, id).done, true, `${id} must be done once ${milestone} is reached`);
    for (const [other] of OLD_MAPPING.filter(([o]) => o !== id)) {
      assert.equal(
        defFor(defs, other).done,
        false,
        `only ${id} should be done when ${milestone} is the sole milestone reached, but ${other} is too`,
      );
    }
  }
});

test("every journey view still gets its live chip", () => {
  // The chip stays a per-view dispatch — decided deliberately, and the issue
  // asks for that decision explicitly. Chip text reads live per-tick state
  // (j.active, wav_count) a static table cannot carry, and buildChip's
  // no-default fall-through is a tsc TS2366 exhaustiveness tripwire that a table
  // lookup would trade for a runtime hole. This pins only that a chip is still
  // produced; HOW stays the plan's choice.
  //
  // Deliberately on the CLEAN table: buildChip has no case for an injected id
  // and returns undefined by design, so an injected view has no chip to assert.
  exported();
  for (const d of spine.journeyDefs(STATE, sessionWith("captured"))) {
    assert.equal(typeof d.chip?.text, "string", `${d.id} lost its chip`);
    assert.ok(d.chip.text.length > 0, `${d.id}'s chip text is empty`);
    assert.ok(
      ["live", "good", "warn", "mute"].includes(d.chip.tone),
      `${d.id}'s chip tone ${JSON.stringify(d.chip.tone)} is not one of the four`,
    );
  }
});

test("spine.js no longer decides `done` by branching on the view id", () => {
  // #252's contract HAD a rung for this and it never fired: it grepped for
  // `id: "<id>"` (the object-literal descriptors it was retiring) while this
  // chain spells `id === "<id>"`. The pin measured spelling, not the property.
  const spineSrc = src("./spine.js");
  const branched = [...ALL_VIEWS].filter((id) => spineSrc.includes(`id === "${id}"`));
  assert.deepEqual(
    branched,
    [],
    `spine.js still branches on ${branched.join(", ")} to set a per-stage flag — the chain #411 names`,
  );
});

test("the journey is still the same four views, in order", () => {
  // Guardrail, green before AND after: every rung above keys off JOURNEY_VIEWS,
  // so a "consolidation" that dropped or reordered a stage would satisfy them all.
  assert.deepEqual([...JOURNEY_VIEWS], ["capture", "recordings", "transcript", "summary"]);
  assert.deepEqual([...ALL_VIEWS], [...GLOBAL_VIEWS, ...JOURNEY_VIEWS]);
});
