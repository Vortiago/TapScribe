// Unit tests for the Stages header gate (run via `node --test`, no DOM).
//
// headerNeedsRender is the per-tick rebuild gate factored out of header() so it
// can be exercised without a browser — `host` is only a WeakMap key, so a plain
// {} stands in. shell.js is import-safe in Node: it only touches document/tpl
// inside functions (same as its sibling *.test.js files). This file is excluded
// from the frontend tsconfig, so the {}-as-host shortcut never hits tsc.

import { test } from "node:test";
import assert from "node:assert/strict";

import { header, headerNeedsRender, nextRecordingEnabled } from "./shell.js";

test("headerNeedsRender rebuilds on the first sig for a fresh host, then skips a repeat", () => {
  const host = {};
  assert.equal(headerNeedsRender(host, "a", false), true); // first ever → rebuild + record
  assert.equal(headerNeedsRender(host, "a", false), false); // unchanged → skip
});

test("headerNeedsRender rebuilds when the sig changes", () => {
  const host = {};
  headerNeedsRender(host, "a", false);
  assert.equal(headerNeedsRender(host, "b", false), true);
  assert.equal(headerNeedsRender(host, "b", false), false); // and settles again
});

test("headerNeedsRender keeps per-host state independent", () => {
  const h1 = {};
  const h2 = {};
  headerNeedsRender(h1, "a", false);
  assert.equal(headerNeedsRender(h2, "a", false), true); // h2 never saw "a"
});

test("headerNeedsRender always rebuilds with actions and clears the recorded sig", () => {
  const host = {};
  headerNeedsRender(host, "a", false); // record "a"
  assert.equal(headerNeedsRender(host, "a", true), true); // actions → always rebuild
  // The actions render cleared "a", so a following gated render with the SAME
  // sig must not falsely skip — the fresh-listener Node had no string signature.
  assert.equal(headerNeedsRender(host, "a", false), true);
});

// --- header() builds its lazy `sub` ONLY past the gate: `{ sig, build }`.build()
// runs on a real rebuild, never on an unchanged tick.
//
// This is the #246 regression guard. The GATED path returns before any
// tpl()/document access, so the skip case runs under node --test with no DOM.
// Priming the gate mirrors header()'s own `${eyebrow}§${title}§${sig}` key so a
// matching call is a guaranteed skip. The rebuild case DOES reach tpl() (which
// needs a DOM) — we run it under try/catch and assert only that build() was
// reached, so the test doesn't hinge on tpl() throwing.

test("header() does not call sub.build() on an unchanged tick", () => {
  const host = {};
  headerNeedsRender(host, "Eb§Ti§K", false); // prime: matches header()'s key for sig "K"
  let built = false;
  assert.doesNotThrow(() =>
    header(host, {
      eyebrow: "Eb",
      title: "Ti",
      sub: {
        sig: "K",
        build: () => {
          built = true;
          throw new Error("sub built on an unchanged tick");
        },
      },
    }),
  );
  assert.equal(built, false); // build() gated → no throwaway allocation
});

test("header() calls sub.build() when the sig changes (real rebuild)", () => {
  const host = {};
  headerNeedsRender(host, "Eb§Ti§K", false); // primed with the OLD sub key
  let calls = 0;
  try {
    header(host, { eyebrow: "Eb", title: "Ti", sub: { sig: "K2", build: () => (calls++, "x") } });
  } catch {
    // Past the gate header() reaches tpl(), which needs a DOM node --test lacks.
    // Irrelevant here: we only assert build() was reached on the real change.
  }
  assert.equal(calls, 1);
});

// --- nextRecordingEnabled: the recording pill's toggle target, factored out
// of wireRecPill (shared by Capture and Taps) so the branching is
// unit-testable without a DOM. "Armed" (recording_enabled true, or unset,
// which defaults to armed) always targets false; explicitly paused targets
// true.

test("nextRecordingEnabled targets false when currently armed (default/unset/true)", () => {
  assert.equal(nextRecordingEnabled(null), false);
  assert.equal(nextRecordingEnabled({}), false);
  assert.equal(nextRecordingEnabled({ recording_enabled: true }), false);
});

test("nextRecordingEnabled targets true when currently explicitly paused", () => {
  assert.equal(nextRecordingEnabled({ recording_enabled: false }), true);
});
