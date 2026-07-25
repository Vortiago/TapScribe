// @ts-check
// Unit tests for the session WAV-listing source (next/session-files.js).
// DOM-free: the module owns the in-flight set, the cold-vs-stale sentinel, and
// the four-state derivation — none of which needs a document.

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

test("resolve: the cold sentinel is reported as loading, never handed back", () => {
  const src = createFilesSource({ onLoaded: () => {}, load: () => null });
  const got = src.resolve("s1", "sig1");
  assert.deepEqual(got.files, [], "a caller must always get an iterable array");
  assert.equal(got.loading, true);
});

test("resolve: a resolved listing is not loading", () => {
  const files = [{ name: "a.wav" }, { name: "b.wav" }];
  const src = createFilesSource({ onLoaded: () => {}, load: () => files });
  const got = src.resolve("s1", "sig1");
  assert.equal(got.files, files);
  assert.equal(got.loading, false);
});

test("resolve: an empty listing is resolved, not loading", () => {
  // `[]` means "nothing to fetch" (no folder / no WAVs yet) — distinct from the
  // cold `null`. Collapsing the two would make an empty session load forever.
  const src = createFilesSource({ onLoaded: () => {}, load: () => [] });
  const got = src.resolve("s1", "");
  assert.deepEqual(got.files, []);
  assert.equal(got.loading, false);
});

test("resolve: the in-flight set is per source and passed through to the loader", () => {
  /** @type {Set<string>[]} */
  const seen = [];
  const mk = () => createFilesSource({
    onLoaded: () => {},
    load: (_s, _sig, pending) => { seen.push(pending); return []; },
  });
  const a = mk();
  const b = mk();
  a.resolve("s1", "sig1");
  a.resolve("s1", "sig1");
  b.resolve("s1", "sig1");
  assert.equal(seen[0], seen[1], "one source must reuse its own in-flight set across ticks");
  assert.notEqual(seen[0], seen[2], "two sources must not share a set, or one cancels the other's first fetch");
});

test("resolve: onLoaded is handed to the loader untouched", () => {
  let handed = null;
  const onLoaded = () => {};
  const src = createFilesSource({
    onLoaded,
    load: (_s, _sig, _p, onLand) => { handed = onLand; return []; },
  });
  src.resolve("s1", "sig1");
  assert.equal(handed, onLoaded, "the success callback must reach api.js unwrapped");
});
