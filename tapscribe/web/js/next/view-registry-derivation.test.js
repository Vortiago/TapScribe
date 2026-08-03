// RED contract for #252: VIEWS derives everything about the Stages view list.
//
// These tests verify the derivation side of the consolidation — templates
// resolve correctly, viewKey derives from the table, and the template set
// matches disk. Import-safe under node --test.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { pathToFileURL } from "node:url";

import * as shell from "./shell.js";

const src = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const BASE = fileURLToPath(new URL("../../../../web/components/next/", import.meta.url));

test("one exported table answers for every view", () => {
  assert.equal(typeof shell.VIEWS, "object", "shell.js exports VIEWS");
  assert.ok(shell.VIEWS instanceof Map, "VIEWS is a Map");
  assert.equal(shell.VIEWS.size, shell.ALL_VIEWS.length, "VIEWS size matches ALL_VIEWS");
  for (const id of shell.ALL_VIEWS) {
    assert.ok(shell.VIEWS.has(id), `VIEWS has entry for "${id}"`);
  }
});

test("the table carries what the parallel lists carried, so they can be derived from it", () => {
  for (const [id, entry] of shell.VIEWS) {
    assert.equal(typeof entry.name, "string", `${id}.name is a string`);
    assert.ok(entry.name.length > 0, `${id}.name is non-empty`);
    assert.ok(entry.group === "global" || entry.group === "journey", `${id}.group is "global"|"journey"`);
    assert.equal(typeof entry.lead, "string", `${id}.lead is a string`);
    assert.ok(entry.lead.length > 0, `${id}.lead is non-empty`);
    assert.equal(typeof entry.template, "string", `${id}.template is a string`);
    assert.ok(entry.template.length > 0, `${id}.template is non-empty`);
  }
});

test("the exported view arrays agree with the table, in order", () => {
  const expectedGlobal = ["taps", "sessions", "people", "settings"];
  const expectedJourney = ["capture", "recordings", "transcript", "summary"];
  assert.deepEqual(shell.GLOBAL_VIEWS, expectedGlobal, "GLOBAL_VIEWS is correct");
  assert.deepEqual(shell.JOURNEY_VIEWS, expectedJourney, "JOURNEY_VIEWS is correct");
  assert.deepEqual(shell.ALL_VIEWS, [...expectedGlobal, ...expectedJourney], "ALL_VIEWS is correct");
});

test("every view id resolves to exactly one template URL", () => {
  for (const [id, entry] of shell.VIEWS) {
    assert.ok(
      typeof entry.template === "string" && entry.template.length > 0,
      `${id}.template is a non-empty string`,
    );
  }
});

test("deduped view-template set is exactly 6 unique files", () => {
  const templates = [...shell.VIEWS.values()].map(e => e.template);
  const deduped = [...new Set(templates)];
  assert.equal(deduped.length, 6, `expected 6 unique templates, got ${deduped.length}: ${deduped.join(", ")}`);
});

test("template files exist on disk", () => {
  const templates = [...new Set([...shell.VIEWS.values()].map(e => e.template))];
  // Template URLs like "/web/components/next/taps.html" map to
  // "../../components/next/taps.html" from the test file's directory.
  const tplDir = fileURLToPath(new URL("../../components/next/", import.meta.url));
  for (const tpl of templates) {
    const file = tpl.replace("/web/components/next/", "");
    const absPath = tplDir + file;
    assert.ok(existsSync(absPath), `template file does not exist: ${tpl} → ${absPath}`);
  }
});

test("every view id resolves to a loadable module path", async () => {
  for (const id of shell.ALL_VIEWS) {
    try {
      // Dynamic import under node --test — if the module path is wrong,
      // this throws with a module-not-found error.
      await import(`./views/${id}.js`);
    } catch (e) {
      assert.fail(`${id}: cannot import ./views/${id}.js — ${e.message}`);
    }
  }
});

test("viewKey derives per-session key only for sessionKey entries", () => {
  assert.equal(
    typeof shell.viewKey,
    "function",
    "shell.js exports viewKey as a function",
  );
  const session = { session: "sess_123" };
  const transcriptKey = shell.viewKey("transcript", session);
  assert.ok(
    transcriptKey.startsWith("transcript:sess_123"),
    `transcript carries session: "${transcriptKey}"`,
  );
  const tapsKey = shell.viewKey("taps", session);
  assert.equal(
    tapsKey,
    "taps",
    `non-sessionKey view returns bare id: "${tapsKey}"`,
  );
  const captureKey = shell.viewKey("capture", session);
  assert.equal(captureKey, "capture", "capture returns bare id");
});

test("sessionKey flag exists on exactly transcript", () => {
  const sessionKeyViews = [...shell.VIEWS.entries()]
    .filter(([, e]) => e.sessionKey)
    .map(([id]) => id);
  assert.deepEqual(sessionKeyViews, ["transcript"], "only transcript has sessionKey");
});

test("the view set itself is unchanged", () => {
  assert.deepEqual(
    [...shell.ALL_VIEWS].sort(),
    ["capture", "people", "recordings", "sessions", "settings", "summary", "taps", "transcript"],
    "the eight Stages views must survive the refactor",
  );
  assert.deepEqual([...shell.ALL_VIEWS], [...shell.GLOBAL_VIEWS, ...shell.JOURNEY_VIEWS]);
});
