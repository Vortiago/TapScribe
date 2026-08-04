// RED contract for #252: the Stages view list must have ONE source of truth.
//
// Today a view is described in five hand-synchronised places — `shell.js`'s
// GLOBAL_VIEWS/JOURNEY_VIEWS arrays, `spine.js`'s nav descriptors (id + name +
// lead icon, re-typed), and three sites in `main.js` (the per-view static
// imports, `buildView`'s `view === "…"` if-chain, and the `loadTemplates`
// list). Nothing makes them agree; they agree only because someone kept them
// in step by hand, which is the debt this issue names.
//
// WHY THESE ASSERTIONS AND NOT A BEHAVIOURAL ONE. All eight views mount
// correctly on main, so every "walk the views and assert they render" test —
// node or Playwright — is GREEN at base and pins nothing. The harm only shows
// when a NINTH view is added, and a test cannot add a view without editing the
// source. So this contract pins the two halves of the consolidation that ARE
// observable: one table answers for every view (below), and the parallel
// hand-maintained copies are gone (the two source-shape rungs at the bottom).
// That is deliberately structural. The design — what the table is called, what
// else it carries, how `main.js` consumes it — is the plan's to choose, so the
// registry is found BY SHAPE here, never by name.
//
// `main.js` cannot be imported under `node --test` (it touches `document` at
// module scope: `import()` fails with "document is not defined"), so the two
// rungs that watch it read its source text. `shell.js` and `spine.js` are
// import-safe, like their sibling *.test.js files.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import * as shell from "./shell.js";

const src = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const sorted = (xs) => [...xs].sort().join(",");

/** The single view table, located BY SHAPE: an exported object (or Map) whose
 * keys are exactly the view ids. Finding it this way keeps the contract from
 * dictating what the plan names it or where inside shell.js it sits. */
function findRegistry() {
  for (const [name, value] of Object.entries(shell)) {
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    const entries = value instanceof Map ? [...value.entries()] : Object.entries(value);
    if (entries.length && sorted(entries.map(([k]) => k)) === sorted(shell.ALL_VIEWS)) {
      return { name, entries };
    }
  }
  return null;
}

test("one exported table answers for every view", () => {
  const reg = findRegistry();
  assert.ok(
    reg,
    "shell.js exports no table keyed by exactly the view ids — adding a view still means " +
      "editing every site that lists views by hand, which is the drift #252 is about",
  );
});

test("the table carries what the parallel lists carried, so they can be derived from it", () => {
  const reg = findRegistry();
  assert.ok(reg, "no view table (see the previous test)");
  for (const [id, entry] of reg.entries) {
    assert.equal(typeof entry, "object", `${reg.name}.${id} is not an entry object`);
    // A display name: spine.js re-types one today ("Taps", "Sessions", …).
    // Either spelling — spine.js calls it `name`, the issue's sketch `label`.
    const shown = entry && (entry.label ?? entry.name);
    assert.ok(
      typeof shown === "string" && shown.length > 0,
      `${reg.name}.${id} carries no display name, so spine.js must keep its own copy`,
    );
    // Which spine group it belongs to: shell.js keeps this as two arrays today.
    assert.ok(
      entry && typeof entry.group === "string" && entry.group.length > 0,
      `${reg.name}.${id} carries no spine group, so GLOBAL_VIEWS/JOURNEY_VIEWS cannot be derived`,
    );
  }
});

test("the exported view arrays agree with the table, in order", () => {
  const reg = findRegistry();
  assert.ok(reg, "no view table (see the first test)");
  const inGroup = (g) => reg.entries.filter(([, e]) => e.group === g).map(([id]) => id);
  const groups = [...new Set(reg.entries.map(([, e]) => e.group))];
  assert.equal(groups.length, 2, `expected two spine groups in ${reg.name}, got ${groups.join()}`);
  // Order matters: the spine renders GLOBAL pinned, then the numbered journey.
  const [a, b] = groups.map(inGroup);
  const asList = sorted(a) === sorted(shell.GLOBAL_VIEWS) ? [a, b] : [b, a];
  assert.deepEqual(asList[0], [...shell.GLOBAL_VIEWS], "GLOBAL_VIEWS is not the table's global group");
  assert.deepEqual(asList[1], [...shell.JOURNEY_VIEWS], "JOURNEY_VIEWS is not the table's journey group");
});

test("spine.js no longer keeps its own hand-typed list of views", () => {
  const spine = src("./components/spine.js");
  const named = [...shell.ALL_VIEWS].filter((id) => spine.includes(`id: "${id}"`));
  assert.deepEqual(
    named,
    [],
    `spine.js still spells out ${named.join(", ")} as its own descriptors — a second list to keep in step`,
  );
});

test("main.js no longer branches per view id", () => {
  const main = src("./main.js");
  const branched = [...shell.ALL_VIEWS].filter((id) => main.includes(`view === "${id}"`));
  assert.deepEqual(
    branched,
    [],
    `buildView still has a per-view branch for ${branched.join(", ")} — the if-chain #252 names`,
  );
});

test("the view set itself is unchanged", () => {
  // Guardrail, green before and after: a "consolidation" that quietly drops a
  // view would satisfy every rung above, since they all key off ALL_VIEWS.
  assert.deepEqual(
    [...shell.ALL_VIEWS].sort(),
    ["capture", "people", "recordings", "sessions", "settings", "summary", "taps", "transcript"],
    "the eight Stages views must survive the refactor",
  );
  assert.deepEqual([...shell.ALL_VIEWS], [...shell.GLOBAL_VIEWS, ...shell.JOURNEY_VIEWS]);
});
