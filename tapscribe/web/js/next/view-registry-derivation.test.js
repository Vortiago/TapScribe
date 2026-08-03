// #252, derivation side: what VIEWS must answer for, and what main.js must
// still load once the per-view template list is derived from it.
//
// The consolidation rungs (the table exists, the arrays agree, the parallel
// copies are gone) live in view-registry.test.js and are NOT repeated here.
// What this file adds is the half a shape-only contract cannot see: that
// replacing a hand-maintained list with a derived one did not silently DROP
// anything the old list carried. The derived set is a hypothesis, not a proof.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import * as shell from "./shell.js";

const abs = (rel) => fileURLToPath(new URL(rel, import.meta.url));
const read = (rel) => readFileSync(abs(rel), "utf8");
const TPL_DIR = "../../components/";

/** The template URLs main.js actually hands to loadTemplates: the literals it
 * still lists by hand, plus the per-view set it derives from VIEWS. main.js is
 * not importable under node --test (it touches `document` at module scope), so
 * the hand-listed half is read from its source. */
function loadedTemplates() {
  const main = read("./main.js");
  const call = main.slice(main.indexOf("loadTemplates("));
  const literals = call.slice(0, call.indexOf("\n  ),")).match(/"\/web\/[^"]+\.html"/g) || [];
  return new Set([
    ...literals.map((s) => s.slice(1, -1)),
    ...[...shell.VIEWS.values()].map((e) => e.template),
  ]);
}

test("no template the old hand-maintained list loaded was dropped", () => {
  // The pre-refactor list, verbatim from b6d2db8 (the RED-contract commit).
  // Deriving the per-view half from VIEWS is only safe if the union still
  // covers every file the page needed before — spine.html is the one that
  // actually went missing, and losing it boots a dashboard with no navigation.
  const before = [
    "/web/components/live-feed.html",
    "/web/components/active-taps.html",
    "/web/components/live-channel.html",
    "/web/components/merged-transcript.html",
    "/web/components/config-card.html",
    "/web/components/next/spine.html",
    "/web/components/next/views.html",
    "/web/components/next/recordings.html",
    "/web/components/next/taps.html",
    "/web/components/next/people.html",
    "/web/components/next/sessions.html",
    "/web/components/next/summary.html",
  ];
  const now = loadedTemplates();
  const dropped = before.filter((t) => !now.has(t));
  assert.deepEqual(dropped, [], `main.js no longer loads: ${dropped.join(", ")}`);
});

test("every template id the Stages modules ask for is declared in a file main.js loads", () => {
  // The forward-looking form of the rung above: a NEW view whose template file
  // never reaches loadTemplates throws "template not loaded" on first mount,
  // which no gate below the e2e tier can see. check-slots.mjs does not cover
  // this — it resolves ids against every .html ON DISK, not against the set a
  // page actually loads, so it passed while spine.html was orphaned.
  /** Every file under `dirRel` with `ext`, depth-first, as (relative path, name). */
  const walk = (dirRel, ext, fn) => {
    for (const e of readdirSync(abs(dirRel), { withFileTypes: true })) {
      if (e.isDirectory()) walk(`${dirRel}${e.name}/`, ext, fn);
      else if (e.name.endsWith(ext)) fn(`${dirRel}${e.name}`, e.name);
    }
  };

  const declaredIn = new Map(); // template id -> "/web/components/…" URL
  walk(TPL_DIR, ".html", (path) => {
    const url = path.replace(TPL_DIR, "/web/components/");
    for (const m of read(path).matchAll(/<template[^>]*\sid="([^"]+)"/g)) declaredIn.set(m[1], url);
  });

  const used = new Set();
  walk("./", ".js", (path, name) => {
    if (name.endsWith(".test.js")) return;
    for (const m of read(path).matchAll(/\btpl\("([^"]+)"\)/g)) used.add(m[1]);
  });

  const loaded = loadedTemplates();
  const unreachable = [...used]
    .filter((id) => declaredIn.has(id) && !loaded.has(declaredIn.get(id)))
    .map((id) => `${id} (in ${declaredIn.get(id)})`);
  assert.deepEqual(unreachable, [], `template ids used but never loaded: ${unreachable.join(", ")}`);
});

test("every entry carries the spine's lead and a template URL", () => {
  // view-registry.test.js pins name + group; these two are what spine.js and
  // the loadTemplates derivation additionally read off the table.
  for (const [id, entry] of shell.VIEWS) {
    assert.ok(typeof entry.lead === "string" && entry.lead.length > 0, `${id}.lead is missing`);
    assert.ok(
      typeof entry.template === "string" && entry.template.startsWith("/web/"),
      `${id}.template is not a template URL: ${entry.template}`,
    );
  }
});

test("the derived arrays are in the spine's render order", () => {
  // Stronger than view-registry.test.js's "the arrays agree with the table":
  // this pins the actual order the spine renders, so reordering VIEWS is a
  // visible change rather than a silent one.
  assert.deepEqual(shell.GLOBAL_VIEWS, ["taps", "sessions", "people", "settings"]);
  assert.deepEqual(shell.JOURNEY_VIEWS, ["capture", "recordings", "transcript", "summary"]);
});

test("template files exist on disk", () => {
  for (const tpl of new Set([...shell.VIEWS.values()].map((e) => e.template))) {
    const path = abs(TPL_DIR + tpl.replace("/web/components/", ""));
    assert.ok(existsSync(path), `template file does not exist: ${tpl} → ${path}`);
  }
});

test("every view id resolves to a loadable module path", async () => {
  // buildView looks the module up by id, so a table entry whose module file is
  // missing degrades to the placeholder view instead of failing loudly.
  for (const id of shell.ALL_VIEWS) {
    await assert.doesNotReject(() => import(`./views/${id}.js`), `cannot import ./views/${id}.js`);
  }
});

test("viewKey derives per-session key only for sessionKey entries", () => {
  const session = { session: "sess_123" };
  assert.equal(shell.viewKey("transcript", session), "transcript:sess_123");
  assert.equal(shell.viewKey("taps", session), "taps");
  assert.equal(shell.viewKey("capture", session), "capture");
});

test("sessionKey flag exists on exactly transcript", () => {
  const flagged = [...shell.VIEWS.entries()].filter(([, e]) => e.sessionKey).map(([id]) => id);
  assert.deepEqual(flagged, ["transcript"]);
});
