// @ts-check
// Unit tests for the lazy-resource mechanism (lazy-resource.js) — the per-tick
// resolve that every /next region reading a lazily-fetched body crosses.
//
// DOM-free and network-free: `load` is injected, so these pin the state machine
// itself (peek-or-fetch-once, in-flight dedupe across watchers, the
// stale-while-revalidate hold, and each failure policy) without a fetch stub.
// api.test.js keeps the integration coverage over the real exported resources.

import test from "node:test";
import assert from "node:assert/strict";

import { createResource } from "./lazy-resource.js";

/** A load whose promises settle on demand: `load` records each call, and
 * `settle(n)` / `reject(n)` resolves the n-th (1-based) one. */
function deferredLoad() {
  /** @type {{ args: unknown[], resolve: (v: any) => void, reject: (e: any) => void }[]} */
  const calls = [];
  const load = (...args) => new Promise((resolve, reject) => { calls.push({ args, resolve, reject }); });
  return {
    load,
    calls,
    settle: (n, value) => { calls[n - 1].resolve(value); },
    reject: (n, err) => { calls[n - 1].reject(err); },
  };
}

/** Drain microtasks so a settled load's .then/.catch bookkeeping has all run. */
const flush = () => new Promise((r) => setTimeout(r, 0));

test("resolve: a settled key is answered from the cache, without a second load", async () => {
  const d = deferredLoad();
  const res = createResource((/** @type {string} */ id) => id, d.load);

  res.fetch("a");
  d.settle(1, { body: "hi" });
  await flush();

  const got = res.watch(() => {}).resolve(["a"]);
  assert.deepEqual(got.value, { body: "hi" });
  assert.equal(got.loading, false);
  assert.equal(got.error, null);
  assert.equal(d.calls.length, 1, "a value already in hand fires no load");
});

test("resolve: a cold miss fires ONE load across ticks and lands ONE repaint", async () => {
  const d = deferredLoad();
  const res = createResource((/** @type {string} */ id) => id, d.load);
  let landed = 0;
  const view = res.watch(() => { landed += 1; });

  // Three poll ticks before the load settles: one request, one placeholder.
  const first = view.resolve(["a"]);
  view.resolve(["a"]);
  view.resolve(["a"]);
  assert.equal(first.value, null);
  assert.equal(first.loading, true);
  assert.equal(d.calls.length, 1, "the in-flight key is not refetched every tick");
  assert.equal(landed, 0);

  d.settle(1, { body: "hi" });
  await flush();
  assert.equal(landed, 1, "one land, one repaint — not one per tick that missed");
  assert.deepEqual(view.resolve(["a"]).value, { body: "hi" });
});

test("watch: two watchers of one key share the load and BOTH get repainted", async () => {
  // The Recordings WAV list and the Transcript picker read the same
  // (session, files_sig). A shared in-flight SET would let the first view's
  // fetch swallow the second view's repaint, which is why each view used to
  // carry its own pending set (and refetch behind the shared cache).
  const d = deferredLoad();
  const res = createResource((/** @type {string} */ id) => id, d.load);
  /** @type {string[]} */
  const landed = [];

  res.watch(() => landed.push("recordings")).resolve(["a"]);
  res.watch(() => landed.push("transcript")).resolve(["a"]);
  assert.equal(d.calls.length, 1, "one key, one load — regardless of watcher count");

  d.settle(1, ["a.wav"]);
  await flush();
  assert.deepEqual(landed.sort(), ["recordings", "transcript"]);
});

test("retry-next-poll: a rejection is silent, skips exactly ONE resolve, then retries", async () => {
  const d = deferredLoad();
  const res = createResource((/** @type {string} */ id) => id, d.load);
  let landed = 0;
  const view = res.watch(() => { landed += 1; });

  view.resolve(["a"]);
  d.reject(1, new Error("500 boom"));
  await flush();
  // No repaint: nothing changed to render, and the repaint's own synchronous
  // re-entry into resolve refiring the evicted fetch WAS the unpaced retry storm.
  assert.equal(landed, 0);

  const paced = view.resolve(["a"]);
  assert.equal(d.calls.length, 1, "the tick right after a failure is skipped — that IS the pacing");
  assert.equal(paced.loading, true);
  assert.equal(paced.error, null, "this policy keeps the failure to itself");

  view.resolve(["a"]);
  assert.equal(d.calls.length, 2, "the next tick retries");
});

test("remember-error: the failure is surfaced, repainted once, and not refetched until the key changes", async () => {
  const d = deferredLoad();
  const res = createResource(
    (/** @type {string} */ id, /** @type {string} */ sig) => `${id}@${sig}`,
    d.load,
    { onFailure: "remember-error" },
  );
  let landed = 0;
  const view = res.watch(() => { landed += 1; });

  view.resolve(["a", "1"]);
  const boom = new Error("500 could not read WAV");
  d.reject(1, boom);
  await flush();
  assert.equal(landed, 1, "unlike retry-next-poll, the error message IS something to render");

  const shown = view.resolve(["a", "1"]);
  assert.equal(shown.error, boom, "the caller formats the message it shows");
  assert.equal(shown.loading, false, "an unreadable body is not 'still loading'");
  assert.equal(d.calls.length, 1, "no retry: the message stands until the key changes");

  view.resolve(["a", "2"]);
  assert.equal(d.calls.length, 2, "a new signature is a new key — it fetches at once");
});

test("holdKeyOf: a signature flip resolves the last-good body, not the cold sentinel (#266)", async () => {
  // files_sig flips once per TRACK during a batch transcribe. Reporting the
  // refetch as a cold load blanks the whole multi-item region to "loading…" once
  // per sibling change — the blink #266 is about.
  const d = deferredLoad();
  const res = createResource(
    (/** @type {string} */ sid, /** @type {string} */ sig) => `${sid}@${sig}`,
    d.load,
    { holdKeyOf: (sid) => sid },
  );
  const view = res.watch(() => {});

  assert.equal(view.resolve(["s1", "sig1"]).loading, true, "a cold load IS a cold load");
  d.settle(1, ["a.wav"]);
  await flush();

  const stale = view.resolve(["s1", "sig2"]);
  assert.deepEqual(stale.value, ["a.wav"], "the previous listing holds while the new sig refetches");
  assert.equal(stale.loading, false, "there is something to show — the region reconciles in place");

  const other = view.resolve(["s2", "sig1"]);
  assert.equal(other.value, null, "the hold is per hold-key — another session never resolved");
  assert.equal(other.loading, true);
});

test("holdKeyOf: a body that resolves to null is reported, never held as stale", async () => {
  // A session with no merged transcript resolves null. Holding that as a
  // "last-good body" would make the next sig flip show a null-shaped stale value;
  // reporting it as loading would make an untranscribed session load forever.
  const d = deferredLoad();
  const res = createResource(
    (/** @type {string} */ sid, /** @type {string} */ sig) => `${sid}@${sig}`,
    d.load,
    { holdKeyOf: (sid) => sid },
  );
  const view = res.watch(() => {});

  view.resolve(["s1", "sig1"]);
  d.settle(1, null);
  await flush();

  const got = view.resolve(["s1", "sig1"]);
  assert.equal(got.value, null);
  assert.equal(got.loading, false, "resolved-to-null is an answer, not a pending fetch");
  assert.equal(view.resolve(["s1", "sig2"]).loading, true, "nothing good was ever held");
});

test("knownValue: an answer needing no fetch resolves at once AND becomes the last-good body", async () => {
  const d = deferredLoad();
  const res = createResource(
    (/** @type {string} */ sid, /** @type {string} */ sig) => `${sid}@${sig}`,
    d.load,
    { holdKeyOf: (sid) => sid, knownValue: (_sid, sig) => (sig ? undefined : []) },
  );
  const view = res.watch(() => {});

  view.resolve(["s1", "sig1"]);
  d.settle(1, ["a.wav"]);
  await flush();

  // Every WAV deleted: files_sig is "" and the answer is definitively empty.
  const emptied = view.resolve(["s1", ""]);
  assert.deepEqual(emptied.value, []);
  assert.equal(emptied.loading, false);
  assert.equal(d.calls.length, 1, "there is nothing to fetch");

  // A new tap records in. While the new sig's listing is in flight the stale path
  // must show the EMPTY listing — resurrecting the deleted rows as ghosts is what
  // seeding the hold above prevents.
  assert.deepEqual(view.resolve(["s1", "sig2"]).value, []);
});

// ---- Regressions found by the #222 code review -------------------------------

test("holdKeyOf: a SLOWER older signature landing late does not reconcile the hold backward", () => {
  // Two fetches for one session are in flight whenever a signature flips faster
  // than a round trip — the #266 case itself (files_sig flips once per track
  // during a batch transcribe). If the older response overwrites the hold, the
  // NEXT flip serves it: a WAV deleted between the two comes back as a ghost row.
  const d = deferredLoad();
  const res = createResource(
    (/** @type {string} */ sid, /** @type {string} */ sig) => `${sid}@${sig}`,
    d.load,
    { holdKeyOf: (sid) => sid },
  );
  const view = res.watch(() => {});

  view.resolve(["s1", "sigA"]); // fired first…
  view.resolve(["s1", "sigB"]); // …but this one answers first
  d.settle(2, [{ name: "b.wav" }]);
  d.settle(1, [{ name: "a-OLD.wav" }]);
  return flush().then(() => {
    assert.deepEqual(
      view.resolve(["s1", "sigC"]).value,
      [{ name: "b.wav" }],
      "the hold must be the NEWEST response received, not the last one to arrive",
    );
  });
});

test("retry-next-poll: the watcher that consumes the skip is the one that fires the retry", async () => {
  // The failure memory is per watcher: if one view's skip paid for another view's
  // fetch, the first would never be in the retry's waiting set and would never get
  // its land — leaving its render gate un-invalidated over stale rows.
  const d = deferredLoad();
  const res = createResource((/** @type {string} */ id) => id, d.load);
  let landedA = 0;
  let landedB = 0;
  const a = res.watch(() => { landedA += 1; });
  const b = res.watch(() => { landedB += 1; });

  a.resolve(["k"]);
  d.reject(1, new Error("endpoint restarting"));
  await flush();

  // B has never failed, so its first resolve fetches rather than skipping.
  b.resolve(["k"]);
  assert.equal(d.calls.length, 2);
  d.settle(2, { body: "ok" });
  await flush();
  assert.equal(landedB, 1);
  assert.equal(landedA, 0, "A was not waiting on B's fetch");

  // A's own memory is still unconsumed, so A skips once and then re-reads the
  // settled cache — it must never be left waiting on a fetch nobody will fire.
  assert.deepEqual(a.resolve(["k"]).value, { body: "ok" }, "a settled cache beats the failure memory");
});

test("remember-error: a fresh watcher retries a key an older watcher gave up on", async () => {
  // A view REBUILD (viewCache.clear() once the model catalogs land) makes a fresh
  // watcher. That is what lets a transient boot-time 503 on /peaks be retried
  // instead of pinning "503 …" on the canvas for the life of the tab.
  const d = deferredLoad();
  const res = createResource((/** @type {string} */ id) => id, d.load, { onFailure: "remember-error" });

  const first = res.watch(() => {});
  first.resolve(["wav"]);
  d.reject(1, new Error("503 warming up"));
  await flush();
  assert.ok(first.resolve(["wav"]).error, "the view that saw the failure keeps showing it");
  assert.equal(d.calls.length, 1);

  const rebuilt = res.watch(() => {});
  assert.equal(rebuilt.resolve(["wav"]).error, null, "a rebuilt view starts from nothing remembered");
  assert.equal(d.calls.length, 2, "and retries the key");
});

test("done: one watcher's throwing repaint does not swallow the others' land", async () => {
  const d = deferredLoad();
  const res = createResource((/** @type {string} */ id) => id, d.load);
  let landedB = 0;
  res.watch(() => { throw new Error("render blew up"); }).resolve(["k"]);
  res.watch(() => { landedB += 1; }).resolve(["k"]);

  d.settle(1, { body: "hi" });
  await flush();
  assert.equal(landedB, 1, "a co-watcher's land must not be aborted by a failing repaint");
});

test("a load that throws SYNCHRONOUSLY lands on the failure path, not stranded in flight", async () => {
  // Every declared load calls encodeURIComponent first, which raises URIError on a
  // lone surrogate. If the throw escaped, the key would stay registered in flight
  // forever and the region would sit at its placeholder for the life of the tab.
  let calls = 0;
  const res = createResource(
    (/** @type {string} */ id) => id,
    (id) => {
      calls += 1;
      if (calls === 1) throw new URIError("URI malformed");
      return Promise.resolve({ id });
    },
  );
  const view = res.watch(() => {});

  assert.equal(view.resolve(["k"]).loading, true, "the throw must not escape to the render pass");
  await flush();
  view.resolve(["k"]); // paced skip, exactly like any other rejection
  view.resolve(["k"]); // retries for real
  assert.equal(calls, 2);
  await flush();
  assert.deepEqual(view.resolve(["k"]).value, { id: "k" });
});

test("a body that resolves to undefined is an answer, not an unbounded re-notify loop", async () => {
  // `settled` is the flag to read, not `value !== undefined`. Otherwise the land
  // notifies a watcher that re-resolves synchronously, misses again, and the two
  // ping-pong through microtasks with no network activity to explain it.
  const d = deferredLoad();
  const res = createResource((/** @type {string} */ id) => id, d.load);
  let landed = 0;
  const view = res.watch(() => { landed += 1; view.resolve(["k"]); });

  view.resolve(["k"]);
  d.settle(1, undefined);
  await flush();
  assert.equal(landed, 1, "one land, not a microtask ping-pong");
  const got = view.resolve(["k"]);
  assert.equal(got.value, null);
  assert.equal(got.loading, false, "settled-as-undefined is reported, never re-fetched");
  assert.equal(d.calls.length, 1);
});

test("the in-flight registration is RELEASED on success, so a later evicted key refetches", async () => {
  // The assertion the rewrite dropped (`pending.size === 0` in api.test.js): if
  // `waiting` were not cleared on the success path, every later resolve would join
  // a wait nobody will settle, and once the cache entry is evicted the region sits
  // on its hold forever with no request in flight.
  const d = deferredLoad();
  const res = createResource((/** @type {string} */ id) => id, d.load);
  const view = res.watch(() => {});

  view.resolve(["k"]);
  d.settle(1, { body: "first" });
  await flush();

  // Push the settled entry out of the bounded cache with unrelated keys.
  for (let i = 0; i < 200; i++) view.resolve([`filler${i}`]);
  assert.equal(res.peek("k"), undefined, "the entry really was evicted");

  const before = d.calls.length;
  view.resolve(["k"]);
  assert.equal(d.calls.length, before + 1, "an evicted key must fetch again, not join a dead wait");
});

test("stale: a provisional body is flagged, so a signature-keyed render gate can see the swap", async () => {
  // The two WAV lists gate on a sig that contains files_sig. Showing the last-good
  // rows under sig B and then the REAL sig-B rows carries no other signature
  // change, so without this term the fresh rows are never reconciled in.
  const d = deferredLoad();
  const res = createResource(
    (/** @type {string} */ sid, /** @type {string} */ sig) => `${sid}@${sig}`,
    d.load,
    { holdKeyOf: (sid) => sid },
  );
  const view = res.watch(() => {});

  assert.equal(view.resolve(["s1", "A"]).stale, false, "a cold load has nothing provisional to show");
  d.settle(1, [{ name: "a.wav" }]);
  await flush();
  assert.equal(view.resolve(["s1", "A"]).stale, false);

  assert.equal(view.resolve(["s1", "B"]).stale, true, "B's rows are not in yet — these are A's");
  d.settle(2, [{ name: "b.wav" }]);
  await flush();
  assert.equal(view.resolve(["s1", "B"]).stale, false, "now they are B's own");
});

test("fetch: a synchronous throw from load is a rejection for DIRECT callers too", async () => {
  // The one-shot expand handler (recordings.js `fillExpand`) calls `fetch`
  // itself, not through a watcher. An escaping synchronous throw lands in a click
  // listener the dashboard swallows, leaving the row on "loading…" forever with
  // no error and no retry — so the guard belongs in `fetch`, not at one caller.
  const res = createResource(
    (/** @type {string} */ id) => id,
    () => { throw new URIError("URI malformed"); },
  );
  await assert.rejects(res.fetch("k"), /URI malformed/);
  assert.equal(res.peek("k"), undefined, "a rejected key is evicted, so a later call retries");
});
