// @ts-check
// Unit tests for the session WAV-listing source (next/session-files.js).
// DOM-free: the module owns the shape of the ANSWER (the cold sentinel is never
// handed back as a value a caller could iterate) and the four-state derivation —
// neither needs a document, and an injected `source` stands in for api.js's
// `sessionFiles` resource.

import test from "node:test";
import assert from "node:assert/strict";

import { createFilesSource, listState } from "./session-files.js";

test("listState: no session beats every other state", () => {
  assert.equal(listState({ hasSession: false, loading: true, count: 5 }), "none");
  assert.equal(listState({ hasSession: false, loading: false, count: 0 }), "none");
});

test("listState: a cold load beats emptiness", () => {
  // The precedence that matters: getting this backwards shows "no recordings
  // yet" during a cold fetch, which is a WRONG answer, not merely a slow one.
  assert.equal(listState({ hasSession: true, loading: true, count: 0 }), "loading");
});

test("listState: rows when there are any, empty when there are none", () => {
  assert.equal(listState({ hasSession: true, loading: false, count: 3 }), "rows");
  assert.equal(listState({ hasSession: true, loading: false, count: 0 }), "empty");
});

/** A stand-in for api.js's `sessionFiles` resource: one canned `resolve` answer,
 * plus an optional spy on what the source was asked for. */
const fakeSource = (answer, spy = (/** @type {unknown[]} */ _a, /** @type {any} */ _o) => {}) => ({
  watch: (/** @type {any} */ onLand) => ({
    resolve: (/** @type {any} */ args) => { spy(args, onLand); return answer; },
  }),
});

test("resolve: the cold sentinel is reported as loading, never handed back", () => {
  const src = createFilesSource({
    onLoaded: () => {},
    source: fakeSource({ value: null, loading: true, error: null }),
  });
  const got = src.resolve("s1", "sig1");
  assert.deepEqual(got.files, [], "a caller must always get an iterable array");
  assert.equal(got.loading, true);
});

test("resolve: a resolved listing is not loading", () => {
  const files = [{ name: "a.wav" }, { name: "b.wav" }];
  const src = createFilesSource({
    onLoaded: () => {},
    source: fakeSource({ value: files, loading: false, error: null }),
  });
  const got = src.resolve("s1", "sig1");
  assert.equal(got.files, files);
  assert.equal(got.loading, false);
});

test("resolve: an empty listing is resolved, not loading", () => {
  // `[]` means "nothing to fetch" (no folder / no WAVs yet) — distinct from the
  // cold `null`. Collapsing the two would make an empty session load forever.
  const src = createFilesSource({
    onLoaded: () => {},
    source: fakeSource({ value: [], loading: false, error: null }),
  });
  const got = src.resolve("s1", "");
  assert.deepEqual(got.files, []);
  assert.equal(got.loading, false);
});

test("resolve: sigTerm carries the stamp, and marks it when the rows are provisional", () => {
  // One owner for the spelling: a view that spliced its own term (or omitted it)
  // would stop reconciling the held-rows -> own-rows swap, silently.
  const fresh = createFilesSource({
    onLoaded: () => {},
    source: fakeSource({ value: [{ name: "a.wav" }], loading: false, stale: false, error: null }),
  });
  assert.equal(fresh.resolve("s1", "sig1").sigTerm, "sig1");

  const held = createFilesSource({
    onLoaded: () => {},
    source: fakeSource({ value: [{ name: "a.wav" }], loading: false, stale: true, error: null }),
  });
  const term = held.resolve("s1", "sig2").sigTerm;
  assert.notEqual(term, "sig2", "held rows must not share a signature with sig2's own rows");
  assert.ok(term.startsWith("sig2"), `the stamp stays readable in the term: ${term}`);
});

test("resolve: (session, files_sig) is the resource's key, passed through in that order", () => {
  /** @type {unknown[][]} */
  const seen = [];
  const src = createFilesSource({
    onLoaded: () => {},
    source: fakeSource({ value: [], loading: false, error: null }, (args) => { seen.push(args); }),
  });
  src.resolve("s1", "sig1");
  assert.deepEqual(seen, [["s1", "sig1"]]);
});

test("resolve: onLoaded is BOUND to the resource unwrapped, once for the source's life", () => {
  /** @type {unknown[]} */
  const bound = [];
  const onLoaded = () => {};
  const src = createFilesSource({
    onLoaded,
    source: fakeSource({ value: [], loading: false, error: null }, (_a, onLand) => { bound.push(onLand); }),
  });
  src.resolve("s1", "sig1");
  src.resolve("s1", "sig1");
  // Every tick resolves through the SAME watcher: the resource dedupes waiting
  // callbacks by identity, and binding at construction is what makes a
  // fresh-closure-per-tick (one repaint per missed tick) unwritable here.
  assert.deepEqual(bound, [onLoaded, onLoaded]);
});
